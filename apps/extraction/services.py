"""Dispatch and ingestion of extraction runs (IP-018 Phase 1).

No extraction happens here. `queue` hands an acquisition to the analyzer
service over Celery; the services post their results back through
`apps.extraction.views`, which call the `report_*` functions below.
"""

import logging
import shutil
import uuid
from typing import Iterable, List, Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.models import Acquisition
from apps.extraction.models import ExtractionRun, Paragraph, ParagraphLink, Region, Sentence
from apps.files.storage import get_storage
from evil_flowers_catalog import domain_metrics, tracing
from evil_flowers_catalog.celery import app

logger = logging.getLogger(__name__)

_ANALYZER_TASK_NAME = "evilflowers_analyzer.extract"
#: Namespace of the analyzer task ids. Fixed: changing it changes every task id.
_TASK_ID_NAMESPACE = uuid.UUID("5b0e8a52-7c1d-4f3e-9a6b-2d8f4c1e7a90")


class RunNotRunning(Exception):
    """Results for a run that was not started, or has already finished."""


class DuplicateParagraphs(Exception):
    """A batch repeats a paragraph `seq` the run already holds."""


def is_extractable(acquisition: Acquisition) -> bool:
    return (
        acquisition.mime == Acquisition.AcquisitionMIME.PDF
        and acquisition.storage_backend == Acquisition.StorageBackend.LOCAL
        and bool(acquisition.content)
    )


@tracing.traced(
    "extraction.queue",
    lambda acquisition, **kwargs: {
        tracing.ACQUISITION_ID: acquisition.pk,
        tracing.ENTRY_ID: acquisition.entry_id,
        "evilflowers.extraction.force": bool(kwargs.get("force")),
    },
)
def queue(acquisition: Acquisition, *, force: bool = False) -> Optional[ExtractionRun]:
    """Queue extraction of `acquisition` by the current pipeline version.

    A run of this version that is queued, running or done is left alone unless
    `force` (a replaced file, a backfill of a run that never finished); a failed
    one is retried. The task is sent once the transaction commits, so a service
    never reports to a run it cannot see yet. Never raises: extraction is
    best-effort and must not fail an upload.
    """
    if not settings.EVILFLOWERS_EXTRACTION_ENABLED or not is_extractable(acquisition):
        return None

    version = settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION
    try:
        run, created = ExtractionRun.objects.get_or_create(acquisition=acquisition, pipeline_version=version)
        tracing.set_attributes(
            {
                tracing.EXTRACTION_RUN_ID: run.pk,
                tracing.EXTRACTION_PIPELINE_VERSION: version,
                "evilflowers.extraction.previous_status": None if created else run.status,
            }
        )
        if not created:
            if run.status != ExtractionRun.Status.FAILED and not force:
                tracing.set_attributes({"evilflowers.extraction.decision": "kept"})
                return run
            _reset(run)
    except Exception:
        logger.exception("Failed to create extraction run for acquisition_id=%s", acquisition.pk)
        return None
    tracing.set_attributes({"evilflowers.extraction.decision": "queued" if created else "requeued"})

    kwargs = {
        "run_id": str(run.pk),
        "acquisition_id": str(acquisition.pk),
        "source": acquisition.content.name,
        "output_dir": run.storage_dir,
        "pipeline_version": version,
    }
    transaction.on_commit(lambda: _send(kwargs))
    return run


def task_id(acquisition_id, pipeline_version: int) -> str:
    """The Celery task id of the analyzer task for one acquisition and pipeline version.

    Deterministic, so the admin can find the task in the broker, and a UUID, because
    every consumer of Celery task ids assumes one: `JobProtocol.id`/`parent` are UUID
    columns and a non-UUID id crashed the event listener (#108).
    """
    return str(uuid.uuid5(_TASK_ID_NAMESPACE, f"extract:{acquisition_id}:v{pipeline_version}"))


def _send(kwargs: dict) -> None:
    task_id_ = task_id(kwargs["acquisition_id"], kwargs["pipeline_version"])
    try:
        # Runs on commit, after `extraction.queue` ended: the PRODUCER span gets a parent that names the run,
        # so the analyzer's CONSUMER span hangs under a step carrying the same `run_id` it reports (IP-029).
        with tracing.span(
            "extraction.dispatch",
            {
                tracing.EXTRACTION_RUN_ID: kwargs["run_id"],
                tracing.ACQUISITION_ID: kwargs["acquisition_id"],
                tracing.EXTRACTION_PIPELINE_VERSION: kwargs["pipeline_version"],
                "messaging.message.id": task_id_,
            },
        ):
            app.send_task(
                _ANALYZER_TASK_NAME,
                kwargs=kwargs,
                queue=settings.EVILFLOWERS_EXTRACTION_QUEUE,
                task_id=task_id_,
            )
    except Exception:
        logger.exception("Failed to queue extraction: task_id=%s run_id=%s", task_id_, kwargs["run_id"])
        return
    logger.info("Queued extraction: task_id=%s run_id=%s", task_id_, kwargs["run_id"])


def _reset(run: ExtractionRun) -> None:
    """Back to `queued`, dropping whatever a previous attempt of this version stored."""
    with transaction.atomic():
        _delete_results(run)
        run.status = ExtractionRun.Status.QUEUED
        run.is_active = False
        run.jobs = {}
        run.error = None
        run.started_at = None
        run.finished_at = None
        run.save()
    delete_files(run.storage_dir)


def _delete_results(run: ExtractionRun) -> None:
    run.regions.all().delete()
    run.paragraphs.all().delete()


def start(
    run: ExtractionRun, *, page_count: int, page_sizes: list, jobs: Iterable[str], provenance: dict
) -> ExtractionRun:
    """The analyzer opened the document and names the jobs it dispatched.

    A run that is already `running` is an analyzer pass done again (a redelivered
    task, a restarted pod): what the earlier pass stored is dropped, or the new
    pass collides with its paragraphs (#120). The crops are left in place; the
    new pass writes the same names over them.

    Known gap: a job of the earlier pass still in flight can post regions after
    this reset. Closing that needs an attempt counter on the run.
    """
    with transaction.atomic():
        # Two passes may post `/start` at once; the second waits and then sees the first one's state.
        run.refresh_from_db(from_queryset=ExtractionRun.objects.select_for_update())
        if run.status not in (ExtractionRun.Status.QUEUED, ExtractionRun.Status.RUNNING):
            raise RunNotRunning(run.status)
        if run.status == ExtractionRun.Status.RUNNING:
            logger.warning("Extraction run restarted by the analyzer, dropping earlier results: run_id=%s", run.pk)
            _delete_results(run)

        run.status = ExtractionRun.Status.RUNNING
        run.page_count = page_count
        run.page_sizes = page_sizes
        run.jobs = {job: ExtractionRun.JobStatus.PENDING for job in jobs}
        run.provenance = {**run.provenance, **(provenance or {})}
        run.started_at = timezone.now()
        run.save()
    return run


def _require_running(run: ExtractionRun) -> None:
    if run.status != ExtractionRun.Status.RUNNING:
        raise RunNotRunning(run.status)


def add_regions(run: ExtractionRun, regions: List[dict]) -> int:
    _require_running(run)
    Region.objects.bulk_create(Region(run=run, **region) for region in regions)
    return len(regions)


def add_paragraphs(run: ExtractionRun, paragraphs: List[dict], links: List[dict]) -> int:
    """Store paragraphs with their sentences; links refer to paragraphs by `seq`.

    Paragraphs may arrive in several batches. A link may point at a paragraph of
    an earlier batch, never of a later one: text-service sends links with the
    last batch.
    """
    _require_running(run)
    try:
        with transaction.atomic():
            created = Paragraph.objects.bulk_create(
                Paragraph(run=run, **{key: value for key, value in paragraph.items() if key != "sentences"})
                for paragraph in paragraphs
            )
            Sentence.objects.bulk_create(
                Sentence(paragraph=row, **sentence)
                for row, paragraph in zip(created, paragraphs)
                for sentence in paragraph.get("sentences") or []
            )
            if links:
                seqs = {link["source"] for link in links} | {link["target"] for link in links}
                by_seq = dict(run.paragraphs.filter(seq__in=seqs).values_list("seq", "id"))
                ParagraphLink.objects.bulk_create(
                    (
                        ParagraphLink(
                            source_id=by_seq[link["source"]],
                            target_id=by_seq[link["target"]],
                            kind=link["kind"],
                            score=link.get("score"),
                        )
                        for link in links
                        if link["source"] in by_seq and link["target"] in by_seq
                    ),
                    ignore_conflicts=True,
                )
    except IntegrityError as error:
        # Deterministic: a retry of the same batch fails the same way, so it is a 409, not a 500.
        raise DuplicateParagraphs(str(error)) from error
    return len(created)


def report_job(run: ExtractionRun, job: str, *, failed: bool, error: Optional[str], provenance: dict) -> None:
    """A service finished its job of the run. The last report finishes the run."""
    with transaction.atomic():
        run = ExtractionRun.objects.select_for_update().get(pk=run.pk)
        _require_running(run)
        if failed and run.jobs.get(job) == ExtractionRun.JobStatus.DONE:
            # A duplicate of a job that already succeeded (a redelivered task) must not fail the run.
            logger.warning("Ignoring failure of job %s already done: run_id=%s error=%s", job, run.pk, error)
            return
        run.jobs = {**run.jobs, job: ExtractionRun.JobStatus.FAILED if failed else ExtractionRun.JobStatus.DONE}
        if provenance:
            run.provenance = {**run.provenance, job: provenance}
        if failed and error:
            run.error = "\n".join(filter(None, [run.error, f"{job}: {error}"]))
        if ExtractionRun.JobStatus.PENDING not in run.jobs.values():
            _finish(run)
        run.save()
    # On the callback's server span: which job reported, and whether that finished the run (IP-029).
    tracing.set_attributes(
        {
            tracing.EXTRACTION_JOB: job,
            "evilflowers.extraction.job.failed": failed,
            "evilflowers.extraction.run.status": run.status,
        }
    )


def fail(run: ExtractionRun, error: str) -> None:
    """The analyzer could not open or lay out the document; nothing else will report."""
    run.status = ExtractionRun.Status.FAILED
    run.error = error
    run.finished_at = timezone.now()
    run.save()
    domain_metrics.record_extraction_run("failed", run.started_at, run.finished_at)
    tracing.set_attributes({"evilflowers.extraction.run.status": run.status})


def _finish(run: ExtractionRun) -> None:
    """Make a run whose jobs all succeeded the active one; a failed job fails the run.

    A failed run leaves the previous active run in place, so readers keep what
    they had until a retry (backfill) succeeds.
    """
    run.finished_at = timezone.now()
    if ExtractionRun.JobStatus.FAILED in run.jobs.values():
        run.status = ExtractionRun.Status.FAILED
        domain_metrics.record_extraction_run("failed", run.started_at, run.finished_at)
        return

    domain_metrics.record_extraction_run("done", run.started_at, run.finished_at)

    previous = list(
        ExtractionRun.objects.filter(acquisition_id=run.acquisition_id)
        .exclude(pk=run.pk)
        .select_related("acquisition")
    )
    run.status = ExtractionRun.Status.DONE
    run.is_active = True
    transaction.on_commit(lambda: update_embedding(run.pk))
    for old in previous:
        folder = old.storage_dir
        old.delete()
        transaction.on_commit(lambda folder=folder: delete_files(folder))


def update_embedding(run_id) -> None:
    """The run's publication vector: the mean of its paragraph embeddings (IP-018 Phase 8).

    Cosine similarity does not depend on length, so the mean needs no normalising.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE extraction_run SET embedding = ("
            "SELECT AVG(embedding) FROM extraction_paragraph WHERE run_id = %s AND embedding IS NOT NULL"
            ") WHERE id = %s",
            [run_id, run_id],
        )


def active_run(acquisition_id) -> Optional[ExtractionRun]:
    return ExtractionRun.objects.filter(acquisition_id=acquisition_id, is_active=True).first()


def delete_files(folder: str) -> None:
    """Remove a folder of crops from the storage backend."""
    storage = get_storage()
    try:
        path = storage.path(folder)
    except NotImplementedError:
        _delete_tree(storage, folder)
        return
    shutil.rmtree(path, ignore_errors=True)


def _delete_tree(storage, folder: str) -> None:
    try:
        directories, files = storage.listdir(folder)
    except Exception:
        return
    for name in files:
        storage.delete(f"{folder}/{name}")
    for name in directories:
        _delete_tree(storage, f"{folder}/{name}")


def delete_for_acquisition(acquisition: Acquisition) -> None:
    """Every run's files of an acquisition that is being deleted."""
    delete_files(f"{acquisition.upload_base_path()}/extraction/{acquisition.pk}")
