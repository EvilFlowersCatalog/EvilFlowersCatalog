"""After a run becomes active (IP-030): its cross-references, figure documents, section similarity and graph.

#155: what is done is recorded on the run (`ExtractionRun.post_processed`), and a beat sweep post-processes every
active run whose marker is not `POST_PROCESS_VERSION`. A run that was active before a step existed, a queue lost
to a broker outage or a rollout, and a change to a step (bump the version) all catch up without a command.
"""

import logging
import time
from typing import List

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings

logger = logging.getLogger(__name__)

POST_PROCESS_KIND = "extraction.post_process"

#: Bump when a post-processing step changes: the sweep then re-processes every active run, a batch at a time.
#: 1: references, figure documents and vectors, section similarity (#151), graph projection.
#: 2: locators (#171): keys of the runs active before them, and the graph's run-independent section keys.
#: 3: reference lists (#178): bibliography items, their locators, in-text markers and `CITES` edges.
#: 4: author lists without backtracking (#192): items can split differently, and the runs whose jobs failed on the
#:    old pattern get a new round.
POST_PROCESS_VERSION = 4

#: Seconds the weekly similarity rebuild may take (every pair of every active run, one set of means).
SIMILARITY_REBUILD_TIME_LIMIT = 60 * 60

#: #192: seconds one run's post-processing may take; the hard limit kills the child two minutes later.
POST_PROCESS_SOFT_TIME_LIMIT = settings.EVILFLOWERS_POST_PROCESS_SOFT_TIME_LIMIT
POST_PROCESS_TIME_LIMIT = POST_PROCESS_SOFT_TIME_LIMIT + 120


def stale_delivery(task_id) -> str | None:
    """Why this delivery of `post_process_run` must not run, or None (#192).

    With late acks, a run whose worker died comes back from Redis after the visibility timeout (12 h), long after
    the ledger gave up on it and its job was retried or failed. On dev such redeliveries held a worker slot for an
    hour each, for jobs that had already failed. A delivery runs only while its run is still the job's business:
    no newer run replaced it, and the job was neither failed nor cancelled. A task the ledger does not know (sent
    while the database was down) runs.
    """
    from apps.tasks.domain.values import DesiredState, JobState
    from apps.tasks.models import JobRun

    if not task_id:
        return None
    run = JobRun.objects.filter(pk=task_id).select_related("job__current_run").first()
    if run is None or run.job is None:
        return None
    job, current = run.job, run.job.current_run
    if current is not None and current.pk != run.pk and current.created_at > run.created_at:
        return f"replaced by run {current.pk}"
    if job.desired_state == DesiredState.CANCELLED or job.state == JobState.CANCELLED:
        return "job cancelled"
    # Only this run's own failure: a run published for a failed job (sweep, apply, re-activation) moves the job to
    # `queued` when the publisher commits, and may be received a moment before that.
    if job.state == JobState.FAILED and job.current_run_id == run.pk:
        return "job failed"
    return None


@shared_task(bind=True, soft_time_limit=POST_PROCESS_SOFT_TIME_LIMIT, time_limit=POST_PROCESS_TIME_LIMIT)
def post_process_run(self, run_id: str) -> dict:
    """References, figure documents and vectors (they use the references), cross-book section similarity, then
    the AGE graph.

    Every step is tried. The run is marked post-processed only when all of them succeeded; otherwise the task
    fails, the job is retried (`PostProcessDefinition.max_attempts`) and the sweep picks the run again. Past the
    soft time limit the steps left are skipped and the task fails naming the step that overran (#192).
    """
    from apps.extraction import bibliography, locators, references, regions, similarity
    from apps.extraction.models import ExtractionRun
    from apps.graph import projection

    reason = stale_delivery(self.request.id)
    if reason is not None:
        logger.warning("extraction.post_process_skipped run=%s task=%s reason=%s", run_id, self.request.id, reason)
        return {"skipped": reason}
    run = ExtractionRun.objects.filter(pk=run_id, is_active=True).select_related("acquisition").first()
    if run is None:
        return {}
    attempt = run.attempt
    steps = (
        ("bibliography", bibliography.build),  # before the locators, which key its items (#178)
        ("locators", locators.assign),  # idempotent; done at activation already, here for older runs (#171)
        ("references", references.build),
        ("regions", regions.index_run),  # TEI down: the documents are set, the vectors fail the step
        ("similarity", similarity.compute_run),  # before the graph, which projects its pairs as SIMILAR_TO (#151)
        ("graph", projection.project_run),
    )
    report, failed = {}, []
    for name, step in steps:
        started = time.monotonic()
        logger.info("extraction.post_process_step run=%s step=%s", run_id, name)
        try:
            report[name] = step(run)
        except SoftTimeLimitExceeded:
            logger.error(
                "extraction.post_process_timeout run=%s step=%s limit=%ss", run_id, name, POST_PROCESS_SOFT_TIME_LIMIT
            )
            raise RuntimeError(
                f"Post-processing of run {run_id} exceeded {POST_PROCESS_SOFT_TIME_LIMIT} s in step {name}"
            ) from None
        except Exception:
            logger.exception("Post-processing step %s of run %s failed", name, run_id)
            failed.append(name)
        finally:
            logger.info(
                "extraction.post_process_step_done run=%s step=%s seconds=%.1f",
                run_id,
                name,
                time.monotonic() - started,
            )
    if failed:
        raise RuntimeError(f"Post-processing of run {run_id} failed in: {', '.join(failed)}")
    if report.get("bibliography", {}).get("items"):
        # Crossref for the items with a DOI: network-bound and optional, so its own task (#178).
        try:
            enrich_bibliography.delay(str(run.pk))
        except Exception:
            logger.warning("Could not queue Crossref enrichment of run %s", run.pk)
    # Not when the run was extracted again meanwhile: those results are new and wait for their own pass.
    ExtractionRun.objects.filter(pk=run.pk, is_active=True, attempt=attempt).update(
        post_processed=POST_PROCESS_VERSION
    )
    report["version"] = POST_PROCESS_VERSION
    return report


@shared_task(ignore_result=True, soft_time_limit=30 * 60)
def enrich_bibliography(run_id: str) -> dict:
    """Crossref metadata of a run's reference list items with a DOI (#178); at most 200 lookups per call."""
    from apps.extraction import bibliography

    return bibliography.enrich_run(run_id)


def queue_post_process(run_id) -> None:
    """Queue `post_process_run`; a broker that is down costs the step, never the ingestion request (the sweep
    picks the run up)."""
    try:
        post_process_run.delay(str(run_id))
    except Exception:
        logger.exception("Could not queue post-processing of run %s; the sweep will", run_id)


def in_flight() -> int:
    """Post-processing runs on their way: queued or started in the jobs ledger, or waiting for their retry."""
    from apps.tasks.domain.values import DesiredState, JobState, RunStatus
    from apps.tasks.models import Job, JobRun

    active = [status for status in RunStatus if status.active]
    runs = JobRun.objects.filter(kind=POST_PROCESS_KIND, status__in=active).count()
    retrying = Job.objects.filter(
        kind=POST_PROCESS_KIND, state=JobState.PENDING, desired_state=DesiredState.COMPLETED
    ).count()
    return runs + retrying


def is_exhausted(job) -> bool:
    """The job failed after its attempts for the current `POST_PROCESS_VERSION`.

    A job without a version in its spec (attached to a `.delay()` before #155, or by `queue_post_process`) is
    not: the sweep gives it one more round of attempts under the current version.
    """
    from apps.tasks.domain.values import JobState

    return job.state == JobState.FAILED and (job.spec or {}).get("version") == POST_PROCESS_VERSION


def exhausted_runs() -> List:
    from apps.tasks.domain.values import JobState
    from apps.tasks.models import Job

    return [
        job.subject_id
        for job in Job.objects.filter(kind=POST_PROCESS_KIND, state=JobState.FAILED).only(
            "subject_id", "state", "spec"
        )
        if is_exhausted(job)
    ]


def next_to_post_process(limit: int) -> List:
    """Up to `limit` active runs the sweep should post-process next: most-read publications first, then the
    oldest. A run qualifies when its marker is not `POST_PROCESS_VERSION` and its job is none of:

    - on its way (pending, blocked, running, cancelling): never two runs at once;
    - cancelled by someone: the sweep does not overrule a person;
    - failed after its attempts for this version: it waits for a manual apply or the next version.

    Nor when a run of the kind is queued or started for it in the ledger, whatever its job says: a `.delay()`
    from the run's activation is attached to the job before any worker has reported on it.
    """
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Exists, F, OuterRef, Q

    from apps.extraction.models import ExtractionRun
    from apps.tasks.domain.values import DesiredState, JobState, RunStatus
    from apps.tasks.models import Job, JobRun

    if limit <= 0:
        return []
    jobs = Job.objects.filter(kind=POST_PROCESS_KIND, subject_type=ContentType.objects.get_for_model(ExtractionRun))
    busy = jobs.filter(subject_id=OuterRef("pk")).filter(
        Q(desired_state=DesiredState.CANCELLED)
        | Q(state__in=(JobState.PENDING, JobState.BLOCKED, JobState.RUNNING, JobState.CANCELLING))
    )
    running = JobRun.objects.filter(
        kind=POST_PROCESS_KIND, subject_id=OuterRef("pk"), status__in=[status for status in RunStatus if status.active]
    )
    return list(
        ExtractionRun.objects.filter(is_active=True)
        .exclude(post_processed=POST_PROCESS_VERSION)
        .exclude(pk__in=exhausted_runs())
        .exclude(Exists(busy))
        .exclude(Exists(running))
        .order_by(F("acquisition__entry__popularity").desc(nulls_last=True), F("finished_at").asc(nulls_last=True))
        .values_list("pk", flat=True)[:limit]
    )


@shared_task
def post_process_sweep() -> dict:
    """#155: declare `extraction.post_process` for the next active runs that are not post-processed (beat).

    Tops up to EVILFLOWERS_POST_PROCESS_SWEEP_BATCH runs in flight; each embeds figures through TEI. A rollout
    costs at most the runs in flight: they become `lost`, are retried, and keep no marker until one succeeds.
    """
    from django.conf import settings

    from apps.tasks.contract import DispatchError, declare

    on_the_way = in_flight()
    declared = []
    for run_id in next_to_post_process(settings.EVILFLOWERS_POST_PROCESS_SWEEP_BATCH - on_the_way):
        try:
            declare(POST_PROCESS_KIND, run_id, spec={"version": POST_PROCESS_VERSION})
        except DispatchError as error:
            logger.warning("extraction.post_process_sweep run=%s refused: %s", run_id, error)
            continue
        declared.append(str(run_id))
    return {"declared": len(declared), "on_the_way": on_the_way, "runs": declared}


@shared_task(time_limit=SIMILARITY_REBUILD_TIME_LIMIT, soft_time_limit=SIMILARITY_REBUILD_TIME_LIMIT - 60)
def similarity_rebuild() -> dict:
    """#155: every section pair again with one set of per-language means (weekly, before `graph-rebuild`).

    `compute_run` is right for the run it computes, but the means drift as books arrive. Holds the similarity
    advisory lock, so post-processing runs wait for it.
    """
    from apps.extraction import similarity

    return {"pairs": similarity.rebuild()}
