"""Dispatch and ingestion of extraction runs (IP-018 Phase 1).

No extraction happens here. `queue` hands an acquisition to the analyzer
service over Celery; the services post their results back through
`apps.extraction.views`, which call the `report_*` functions below.
"""

import logging
import shutil
import uuid
from datetime import timedelta
from typing import Iterable, List, Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.models import Acquisition
from apps.extraction import batches
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


class RunSuperseded(RunNotRunning):
    """Results of an earlier attempt of a run that was reset or started again since (#140)."""

    def __init__(self, attempt: int, current: int):
        super().__init__("superseded")
        self.attempt = attempt
        self.current = current


class DuplicateParagraphs(Exception):
    """A batch repeats a paragraph `seq` the run already holds."""


class UnknownJob(Exception):
    """A report for a job `/start` did not declare (#180)."""

    def __init__(self, job: str, declared):
        super().__init__(job)
        self.job = job
        self.declared = sorted(declared)


class StepsRefused(Exception):
    """A step re-run (#183) the run cannot take; the message says why."""


#: Job name the analyzer reports under; never declared by `/start` (it is the one declaring).
ANALYZER_JOB = "analyzer"

STEPS = batches.STEPS
#: The region kind each region step stores. `Region` records no producer: a service posts the kind of its own job
#: (the mapping `views._REGION_JOBS` counts superseded batches by), so `kind` is the producer. The printed caption
#: may disagree (a `figure` captioned "Table 3.4"); that is the layout model's label, not a second producer.
STEP_REGION_KINDS = {"figures": Region.Kind.FIGURE, "tables": Region.Kind.TABLE, "equations": Region.Kind.EQUATION}


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


def step_kwargs(run: ExtractionRun, steps: Iterable[str]) -> dict:
    """The analyzer task's keyword arguments for a step re-run (#183): those of `queue` plus `steps`.

    An analyzer that predates #183 ignores `steps` (`**_`) and posts a full `/start`, which a `failed` run
    refuses with 409: nothing is lost, the run stays failed. That is why the step re-run is gated
    (`EVILFLOWERS_EXTRACTION_STEP_RERUN`): with an old analyzer it would do nothing.
    """
    acquisition = run.acquisition
    return {
        "run_id": str(run.pk),
        "acquisition_id": str(acquisition.pk),
        "source": acquisition.content.name,
        "output_dir": run.storage_dir,
        "pipeline_version": run.pipeline_version,
        "steps": sorted(set(steps)),
    }


def step_plan(run: ExtractionRun, steps: Iterable[str], *, requested: bool = True) -> dict:
    """What a step re-run (#183) of `run` resets and keeps; raises `StepsRefused` when it may not.

    Granularity is the whole step: every job of a named kind is declared again by the analyzer (it may batch
    differently than the pass before), done batches of that kind included. The invariant of the attempt
    fencing (#140) holds only if, after the partial start, every job of the run is either `done` (its data
    kept) or `pending` under the new attempt. So every kind that is not done must be named; a run with another
    unfinished kind is refused rather than silently widened. `requested`: a client's request, which may not
    name a step that is already done; the analyzer's `/start` of a redelivered step pass may (#183).
    """
    steps = sorted(set(steps))
    unknown = [step for step in steps if step not in STEPS]
    if unknown:
        raise StepsRefused(f"Unknown steps: {', '.join(unknown)}; known: {', '.join(STEPS)}.")
    if not steps:
        raise StepsRefused("No step named; an empty list re-runs the whole run.")
    if not run.jobs:
        raise StepsRefused("The run never declared its jobs; re-run it whole.")
    groups = {group.kind: group for group in batches.group_jobs(run.jobs)}
    absent = [step for step in steps if step not in groups]
    if absent:
        raise StepsRefused(f"The run has no {', '.join(absent)} jobs; re-run it whole.")
    if requested:
        done = [step for step in steps if groups[step].counts["done"] == groups[step].total]
        if done:
            raise StepsRefused(f"Already done: {', '.join(done)}. Re-run the whole run to redo a finished step.")
    unfinished = sorted(
        kind for kind, group in groups.items() if kind not in steps and group.counts["done"] < group.total
    )
    if unfinished:
        raise StepsRefused(
            f"Not done either: {', '.join(unfinished)}. Name every unfinished step "
            f"(steps: {', '.join(sorted({*steps, *unfinished}))})."
        )
    return {
        "steps": steps,
        "jobs": sorted(name for name in run.jobs if batches.job_kind(name) in steps),
        "kept": sorted(kind for kind in groups if kind not in steps),
    }


def step_counts(run: ExtractionRun, steps: Iterable[str]) -> dict:
    """What a step re-run deletes: rows per step, for `?dry_run=true`."""
    counts = {}
    for step in steps:
        if step == "paragraphs":
            counts[step] = run.paragraphs.count()
        elif step in STEP_REGION_KINDS:
            counts[step] = run.regions.filter(kind=STEP_REGION_KINDS[step]).count()
    return counts


def _start_steps(run: ExtractionRun, *, steps, page_count: int, page_sizes: list, jobs, provenance: dict) -> None:
    """`/start` of a step re-run (#183), on the run's locked row: reset only the named steps.

    The run must be `failed` (what a client re-runs steps of) or `running` (a redelivered step pass). A step
    pass over a run whose other steps are still pending is refused by `step_plan`. The named steps lose their data and
    their jobs; the jobs the analyzer declares now are `pending` under a new attempt. The other steps keep
    their rows and their `done` status. Every job still out under the old attempt (a stale task of a named
    step) is refused as `superseded` from here on, and so is a duplicate report of a kept step: it is done.

    `is_active` and `post_processed` stay: the run is normally not the active one (only `done` runs are), and
    `_finish` resets the marker, queues post-processing and assigns locators once the steps are back.
    """
    if run.status not in (ExtractionRun.Status.FAILED, ExtractionRun.Status.RUNNING):
        raise RunNotRunning(run.status)
    plan = step_plan(run, steps, requested=False)
    steps = plan["steps"]
    foreign = sorted(name for name in jobs if batches.job_kind(name) not in steps)
    if foreign:
        raise StepsRefused(f"Declared jobs of steps that were not asked for: {', '.join(foreign)}.")
    missing = [step for step in steps if not any(batches.job_kind(name) == step for name in jobs)]
    if missing:
        raise StepsRefused(f"Declared no job for: {', '.join(missing)}.")

    crops = _delete_step_results(run, steps)
    kept = {name: status for name, status in run.jobs.items() if batches.job_kind(name) not in steps}
    run.jobs = {**kept, **{name: ExtractionRun.JobStatus.PENDING for name in jobs}}
    run.status = ExtractionRun.Status.RUNNING
    run.attempt += 1
    run.page_count = page_count
    run.page_sizes = page_sizes
    # The steps' provenance is reported again by their new jobs; a batch would only add a variant to the old one.
    provenance_ = {key: value for key, value in run.provenance.items() if key not in steps}
    deciders = {key: value for key, value in (provenance_.get(DECIDER_MODELS) or {}).items() if key not in steps}
    if DECIDER_MODELS in provenance_:
        provenance_[DECIDER_MODELS] = deciders
    run.provenance = {**provenance_, **(provenance or {})}
    # Only the named steps' failures and the stale reason were in it: those steps start over.
    run.error = None
    run.started_at = timezone.now()
    run.finished_at = None
    run.save()
    transaction.on_commit(lambda: delete_crops(crops))
    logger.info(
        "Extraction steps restarted: run_id=%s steps=%s jobs=%d kept=%s attempt=%s",
        run.pk,
        ",".join(steps),
        len(jobs),
        ",".join(plan["kept"]),
        run.attempt,
    )
    tracing.set_attributes({"evilflowers.extraction.steps": ",".join(steps)})


def _delete_step_results(run: ExtractionRun, steps: Iterable[str]) -> List[str]:
    """Delete what the named steps stored; returns the storage names of the deleted regions' crops.

    `paragraphs` takes its sentences, links and cross-references along (cascades); a region step its kind's
    regions on every page (whole-step granularity), with their cross-references.
    """
    crops: List[str] = []
    kinds = [STEP_REGION_KINDS[step] for step in steps if step in STEP_REGION_KINDS]
    if kinds:
        regions = run.regions.filter(kind__in=kinds)
        crops = [path for path in regions.values_list("image_path", flat=True) if path]
        regions.delete()
    if "paragraphs" in steps:
        run.paragraphs.all().delete()
    return crops


def delete_crops(paths: Iterable[str]) -> None:
    """Remove single crops from the storage backend; best effort."""
    storage = get_storage()
    for path in paths:
        try:
            storage.delete(path)
        except Exception:
            logger.warning("Cannot delete crop %s", path, exc_info=True)


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
        # Locked before the delete: a batch of the attempt being dropped either committed already (and is
        # deleted here) or waits and is refused as superseded (`_require_running_locked`).
        run.refresh_from_db(from_queryset=ExtractionRun.objects.select_for_update())
        _delete_results(run)
        run.status = ExtractionRun.Status.QUEUED
        # Jobs of the attempt being dropped learn it on their next call, not after their whole pass (#140).
        run.attempt += 1
        run.is_active = False
        run.jobs = {}
        run.error = None
        run.started_at = None
        run.finished_at = None
        run.post_processed = None
        run.save()
    delete_files(run.storage_dir)


def _delete_results(run: ExtractionRun) -> None:
    run.regions.all().delete()
    run.paragraphs.all().delete()


def start(
    run: ExtractionRun,
    *,
    page_count: int,
    page_sizes: list,
    jobs: Iterable[str],
    provenance: dict,
    steps: Optional[Iterable[str]] = None,
) -> ExtractionRun:
    """The analyzer opened the document and names the jobs it dispatched.

    With `steps` (#183) it is a step re-run: see `_start_steps`; only those steps are reset.

    A run that is already `running` is an analyzer pass done again (a redelivered
    task, a restarted pod): what the earlier pass stored is dropped, or the new
    pass collides with its paragraphs (#120). The crops are left in place; the
    new pass writes the same names over them.

    Every start is a new `attempt` (#140), returned to the analyzer, which hands
    it to the jobs it dispatches. A job of an earlier pass still in flight is
    refused as `superseded` from here on, and stops early if it polls the run.
    """
    with transaction.atomic():
        # Two passes may post `/start` at once; the second waits and then sees the first one's state.
        run.refresh_from_db(from_queryset=ExtractionRun.objects.select_for_update())
        if steps:
            jobs = list(jobs)
            _start_steps(
                run, steps=steps, page_count=page_count, page_sizes=page_sizes, jobs=jobs, provenance=provenance
            )
            tracing.set_attributes({"evilflowers.extraction.attempt": run.attempt})
            return run
        if run.status not in (ExtractionRun.Status.QUEUED, ExtractionRun.Status.RUNNING):
            raise RunNotRunning(run.status)
        if run.status == ExtractionRun.Status.RUNNING:
            logger.warning("Extraction run restarted by the analyzer, dropping earlier results: run_id=%s", run.pk)
            _delete_results(run)

        run.status = ExtractionRun.Status.RUNNING
        run.attempt += 1
        run.page_count = page_count
        run.page_sizes = page_sizes
        run.jobs = {job: ExtractionRun.JobStatus.PENDING for job in jobs}
        # A full pass reports its deciders again (#189); the previous attempt's would only linger.
        previous = {key: value for key, value in run.provenance.items() if key != DECIDER_MODELS}
        run.provenance = {**previous, **(provenance or {})}
        run.started_at = timezone.now()
        run.save()
    tracing.set_attributes({"evilflowers.extraction.attempt": run.attempt})
    return run


def _require_running(run: ExtractionRun, attempt: Optional[int] = None) -> None:
    """`attempt`: the one the caller works for; `None` from services that predate #140 (not checked)."""
    if attempt is not None and attempt != run.attempt:
        tracing.set_attributes(
            {"evilflowers.extraction.attempt": attempt, "evilflowers.extraction.attempt.current": run.attempt}
        )
        raise RunSuperseded(attempt, run.attempt)
    if run.status != ExtractionRun.Status.RUNNING:
        raise RunNotRunning(run.status)


def check_attempt(run: ExtractionRun, attempt: Optional[int]) -> None:
    """Refuse a result of an earlier attempt even where the run's status is not checked."""
    if attempt is not None and attempt != run.attempt:
        raise RunSuperseded(attempt, run.attempt)


def report_superseded(run: ExtractionRun, job: str, attempt: Optional[int]) -> None:
    """A service saw that its attempt is no longer the run's and stopped before the work (#140).

    Changes nothing: the new attempt's jobs report for themselves. Only counted, so the
    extraction dashboard shows how much work resets cost and how much the early stop saved.
    """
    logger.info(
        "Extraction job superseded, stopped early: run_id=%s job=%s attempt=%s current=%s",
        run.pk,
        job,
        attempt,
        run.attempt,
    )
    tracing.set_attributes(
        {
            tracing.EXTRACTION_JOB: job,
            "evilflowers.extraction.attempt": attempt,
            "evilflowers.extraction.attempt.current": run.attempt,
        }
    )
    domain_metrics.record_extraction_superseded(job, "early")


def _require_running_locked(run: ExtractionRun, attempt: Optional[int]) -> None:
    """`_require_running` on the run's row locked until the caller's transaction ends.

    `/start` and a reset lock the same row, so a batch checked here cannot land in the
    next attempt's results: either it commits first and the reset drops it, or it waits
    and sees the new attempt.
    """
    locked = ExtractionRun.objects.select_for_update().only("id", "status", "attempt").get(pk=run.pk)
    run.status, run.attempt = locked.status, locked.attempt
    _require_running(run, attempt)


#: Key of the run's provenance that lists the deciders behind its regions, per step (#189).
DECIDER_MODELS = "decider_models"


def merge_decider_models(provenance: Optional[dict], regions: List[dict]) -> Optional[dict]:
    """`provenance[decider_models][step]` gains the batch's distinct `decider_model`s; None when nothing is new."""
    steps = {kind: step for step, kind in STEP_REGION_KINDS.items()}
    reported: dict = {}
    for region in regions:
        model, step = region.get("decider_model"), steps.get(region.get("kind"))
        if model and step:
            reported.setdefault(step, set()).add(model)
    if not reported:
        return None
    current = dict((provenance or {}).get(DECIDER_MODELS) or {})
    changed = False
    for step, models in reported.items():
        known = list(current.get(step) or [])
        new = sorted(models - set(known))
        if new:
            current[step] = [*known, *new]
            changed = True
    return {**(provenance or {}), DECIDER_MODELS: current} if changed else None


def add_regions(run: ExtractionRun, regions: List[dict], attempt: Optional[int] = None) -> int:
    with transaction.atomic():
        _require_running_locked(run, attempt)
        Region.objects.bulk_create(Region(run=run, **region) for region in regions)
        if any(region.get("decider_model") for region in regions):
            # Re-read under the lock `_require_running_locked` holds: batches of two jobs may run at once.
            locked = ExtractionRun.objects.only("id", "provenance").get(pk=run.pk)
            provenance = merge_decider_models(locked.provenance, regions)
            if provenance is not None:
                ExtractionRun.objects.filter(pk=run.pk).update(provenance=provenance)
                run.provenance = provenance
    return len(regions)


def add_paragraphs(
    run: ExtractionRun, paragraphs: List[dict], links: List[dict], attempt: Optional[int] = None
) -> int:
    """Store paragraphs with their sentences; links refer to paragraphs by `seq`.

    Paragraphs may arrive in several batches. A link may point at a paragraph of
    an earlier batch, never of a later one: text-service sends links with the
    last batch.
    """
    try:
        with transaction.atomic():
            _require_running_locked(run, attempt)
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


def report_job(
    run: ExtractionRun,
    job: str,
    *,
    failed: bool,
    error: Optional[str],
    provenance: dict,
    attempt: Optional[int] = None,
) -> None:
    """A service finished its job of the run. The last report finishes the run.

    Only jobs `/start` declared (and the analyzer's own) are accepted: a misnamed report would
    add a key, leave the declared job pending until the stale sweep and merge provenance under
    a kind that does not exist (#180).
    """
    with transaction.atomic():
        run = ExtractionRun.objects.select_for_update().get(pk=run.pk)
        _require_running(run, attempt)
        if job != ANALYZER_JOB and job not in run.jobs:
            raise UnknownJob(job, run.jobs)
        if failed and run.jobs.get(job) == ExtractionRun.JobStatus.DONE:
            # A duplicate of a job that already succeeded (a redelivered task) must not fail the run.
            logger.warning("Ignoring failure of job %s already done: run_id=%s error=%s", job, run.pk, error)
            return
        run.jobs = {**run.jobs, job: ExtractionRun.JobStatus.FAILED if failed else ExtractionRun.JobStatus.DONE}
        if provenance:
            # Per kind, not per batch: with per-batch dispatch a run has hundreds of jobs (#150).
            run.provenance = batches.merge_provenance(run.provenance, job, provenance)
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


def fail_stale_runs(idle_hours: float) -> int:
    """Fail the `running` runs no service has reported to for `idle_hours`; returns how many.

    A job whose task was lost never reports, and its run stays `running` for good: `queue`
    keeps a run that has not failed, so a backfill never retries it. Seen on dev in Nov 2026,
    runs waiting a day on a job text-service kept redelivering against a 409. Idle time counts
    from the last report (`updated_at`), not the start, because a long equations queue keeps
    healthy runs waiting for hours.
    """
    cutoff = timezone.now() - timedelta(hours=idle_hours)
    stale = ExtractionRun.objects.filter(status=ExtractionRun.Status.RUNNING, updated_at__lt=cutoff)
    failed = 0
    for run_id in stale.values_list("pk", flat=True):
        with transaction.atomic():
            run = ExtractionRun.objects.select_for_update().get(pk=run_id)
            # A report may have landed since the query.
            if run.status != ExtractionRun.Status.RUNNING or run.updated_at >= cutoff:
                continue
            pending = [job for job, status in run.jobs.items() if status == ExtractionRun.JobStatus.PENDING]
            reason = f"no report for {idle_hours:g} h, pending: {batches.pending_summary(pending)}"
            logger.warning("Extraction run stale, failing it: run_id=%s %s", run.pk, reason)
            fail(run, "\n".join(filter(None, [run.error, reason])))
            failed += 1
    return failed


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
    # New results: post-processed again (#155), here or by the sweep if the queue below is lost.
    run.post_processed = None
    transaction.on_commit(lambda: update_embedding(run.pk))
    # IP-030: cross-references, figure vectors and the graph, off the ingestion request.
    from apps.extraction.tasks import queue_post_process

    transaction.on_commit(lambda: queue_post_process(run.pk))
    for old in previous:
        folder = old.storage_dir
        old.delete()
        transaction.on_commit(lambda folder=folder: delete_files(folder))
    _assign_locators(run)


def _assign_locators(run: ExtractionRun) -> None:
    """#171: the new run's units keep the addresses they had in the old one; what it lacks is detached.

    In a savepoint: a failure here must not fail the ingestion. Post-processing assigns the keys again and
    reports the step failed, so the sweep retries it.
    """
    from apps.extraction import locators

    try:
        with transaction.atomic():
            locators.assign(run)
    except Exception:
        logger.exception("Assigning locators of run %s failed; post-processing retries it", run.pk)


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
