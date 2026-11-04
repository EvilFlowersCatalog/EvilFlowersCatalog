"""Dispatch and ingestion of extraction runs (IP-018 Phase 1).

No extraction happens here. `queue` hands an acquisition to the analyzer
service over Celery; the services post their results back through
`apps.extraction.views`, which call the `report_*` functions below.
"""

import logging
import shutil
from typing import Iterable, List, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.models import Acquisition
from apps.extraction.models import ExtractionRun, Paragraph, ParagraphLink, Region, Sentence
from apps.files.storage import get_storage
from evil_flowers_catalog.celery import app

logger = logging.getLogger(__name__)

_ANALYZER_TASK_NAME = "evilflowers_analyzer.extract"


class RunNotRunning(Exception):
    """Results for a run that was not started, or has already finished."""


def is_extractable(acquisition: Acquisition) -> bool:
    return (
        acquisition.mime == Acquisition.AcquisitionMIME.PDF
        and acquisition.storage_backend == Acquisition.StorageBackend.LOCAL
        and bool(acquisition.content)
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
        if not created:
            if run.status != ExtractionRun.Status.FAILED and not force:
                return run
            _reset(run)
    except Exception:
        logger.exception("Failed to create extraction run for acquisition_id=%s", acquisition.pk)
        return None

    kwargs = {
        "run_id": str(run.pk),
        "acquisition_id": str(acquisition.pk),
        "source": acquisition.content.name,
        "output_dir": run.storage_dir,
        "pipeline_version": version,
    }
    transaction.on_commit(lambda: _send(kwargs))
    return run


def _send(kwargs: dict) -> None:
    task_id = f"extract:{kwargs['acquisition_id']}:v{kwargs['pipeline_version']}"
    try:
        app.send_task(
            _ANALYZER_TASK_NAME,
            kwargs=kwargs,
            queue=settings.EVILFLOWERS_EXTRACTION_QUEUE,
            task_id=task_id,
        )
    except Exception:
        logger.exception("Failed to queue extraction: task_id=%s", task_id)
        return
    logger.info("Queued extraction: task_id=%s run_id=%s", task_id, kwargs["run_id"])


def _reset(run: ExtractionRun) -> None:
    """Back to `queued`, dropping whatever a previous attempt of this version stored."""
    with transaction.atomic():
        run.regions.all().delete()
        run.paragraphs.all().delete()
        run.status = ExtractionRun.Status.QUEUED
        run.is_active = False
        run.jobs = {}
        run.error = None
        run.started_at = None
        run.finished_at = None
        run.save()
    delete_files(run.storage_dir)


def start(run: ExtractionRun, *, page_count: int, page_sizes: list, jobs: Iterable[str], provenance: dict) -> None:
    """The analyzer opened the document and names the jobs it dispatched."""
    if run.status not in (ExtractionRun.Status.QUEUED, ExtractionRun.Status.RUNNING):
        raise RunNotRunning(run.status)

    run.status = ExtractionRun.Status.RUNNING
    run.page_count = page_count
    run.page_sizes = page_sizes
    run.jobs = {job: ExtractionRun.JobStatus.PENDING for job in jobs}
    run.provenance = {**run.provenance, **(provenance or {})}
    run.started_at = timezone.now()
    run.save()


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
    return len(created)


def report_job(run: ExtractionRun, job: str, *, failed: bool, error: Optional[str], provenance: dict) -> None:
    """A service finished its job of the run. The last report finishes the run."""
    with transaction.atomic():
        run = ExtractionRun.objects.select_for_update().get(pk=run.pk)
        _require_running(run)
        run.jobs = {**run.jobs, job: ExtractionRun.JobStatus.FAILED if failed else ExtractionRun.JobStatus.DONE}
        if provenance:
            run.provenance = {**run.provenance, job: provenance}
        if failed and error:
            run.error = "\n".join(filter(None, [run.error, f"{job}: {error}"]))
        if ExtractionRun.JobStatus.PENDING not in run.jobs.values():
            _finish(run)
        run.save()


def fail(run: ExtractionRun, error: str) -> None:
    """The analyzer could not open or lay out the document; nothing else will report."""
    run.status = ExtractionRun.Status.FAILED
    run.error = error
    run.finished_at = timezone.now()
    run.save()


def _finish(run: ExtractionRun) -> None:
    """Make a run whose jobs all succeeded the active one; a failed job fails the run.

    A failed run leaves the previous active run in place, so readers keep what
    they had until a retry (backfill) succeeds.
    """
    run.finished_at = timezone.now()
    if ExtractionRun.JobStatus.FAILED in run.jobs.values():
        run.status = ExtractionRun.Status.FAILED
        return

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
