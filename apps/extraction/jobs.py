"""Extraction job kinds (IP-031): the analyzer run (IP-018), its post-processing (IP-030) and their sweeps (#155).

The run's truth is `ExtractionRun`: the definition observes it, and summarises its jobs per kind
(`figures:0001-0008` → `figures`), because with per-batch dispatch a book has hundreds of them (#150).
"""

from datetime import timedelta
from typing import Literal

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext as _
from pydantic import field_validator, model_serializer

from apps.core.models import Acquisition
from apps.extraction.batches import STEPS, missing_steps, summarise_jobs
from apps.extraction.tasks import (
    POST_PROCESS_KIND,
    POST_PROCESS_VERSION,
    SIMILARITY_REBUILD_TIME_LIMIT,
    is_exhausted,
)
from apps.tasks.contract import (
    Blocked,
    DispatchError,
    JobDefinition,
    JobState,
    Observation,
    Ready,
    Refused,
    Spec,
    entry_scope,
    maintenance,
    registry,
    run_only,
    uuid_arg,
)


def _current_run(acquisition):
    from apps.extraction.models import ExtractionRun

    return ExtractionRun.objects.filter(
        acquisition=acquisition, pipeline_version=settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION
    ).first()


class ExtractionRunSpec(Spec):
    """`{}`: the whole run again. `{"steps": ["paragraphs"]}`: only those steps of a failed run (#183)."""

    #: Steps of the current pipeline version's failed run to re-run; every step that is not done must be named.
    steps: tuple[Literal[STEPS], ...] = ()

    @field_validator("steps")
    @classmethod
    def _normalise(cls, value):
        return tuple(sorted(set(value)))

    @model_serializer(mode="wrap")
    def _omit_empty(self, handler):
        # A full run stays `{}`, the spec every job stored before #183: anything else reads as a new spec and
        # starts a new generation (a second full extraction) on the next idempotent apply.
        data = handler(self)
        if not data.get("steps"):
            data.pop("steps", None)
        return data


@registry.register
class ExtractionRunDefinition(JobDefinition[Acquisition, ExtractionRunSpec]):
    kind = "extraction.run"
    title = "AI processing (extraction)"
    description = (
        "Extract figures, tables, equations and the paragraph index of a PDF (IP-018). The analyzer fans out to "
        "the image, equation and text workers; their tasks are runs of this job. `steps` re-runs only those steps "
        "of a failed run, keeping the others' results (#183, EVILFLOWERS_EXTRACTION_STEP_RERUN)."
    )
    task = "evilflowers_analyzer.extract"
    subject_model = Acquisition
    spec_model = ExtractionRunSpec
    # The task id is a function of acquisition and pipeline version: workers' late reports are told apart
    # by `attempt`, so every run of the job reuses one run row (Q9). A step re-run gets its own (`task_id_for`).
    stable_task_id = True

    @property
    def queue(self):
        return settings.EVILFLOWERS_EXTRACTION_QUEUE

    @property
    def observes(self):
        from apps.extraction.models import ExtractionRun

        return (ExtractionRun,)

    def scope(self, acquisition):
        return entry_scope(acquisition.entry)

    def subject_from_call(self, args, kwargs):
        return uuid_arg([], kwargs, "acquisition_id")

    def subject_of(self, run):
        if run.pipeline_version != settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION:
            return None
        return run.acquisition

    def check(self, acquisition, spec):
        from apps.extraction.services import is_extractable

        if not is_extractable(acquisition):
            return Refused(_("Only stored PDF files are extracted."))
        if not settings.EVILFLOWERS_EXTRACTION_ENABLED:
            message = _("Extraction is switched off (EVILFLOWERS_EXTRACTION_ENABLED).")
            # A blocked step spec would be sent by itself once extraction is on, against whatever the run is then.
            return Refused(message) if spec.steps else Blocked(message)
        if spec.steps:
            return self._check_steps(acquisition, spec)
        return Ready()

    @staticmethod
    def _check_steps(acquisition, spec):
        """Refused, never Blocked: a blocked spec is stored and dispatched by itself once the cause goes away."""
        from apps.extraction import services
        from apps.extraction.models import ExtractionRun

        if not settings.EVILFLOWERS_EXTRACTION_STEP_RERUN:
            return Refused(
                _(
                    "Re-running single steps is switched off (EVILFLOWERS_EXTRACTION_STEP_RERUN): the analyzer in use "
                    "may not support it. Re-run the whole run (empty spec)."
                )
            )
        run = _current_run(acquisition)
        if run is None:
            return Refused(_("No run of the current pipeline version to re-run steps of; run it whole."))
        if run.status != ExtractionRun.Status.FAILED:
            # A running run's job is active: a new generation would wait for a run that already reported.
            return Refused(
                _("Only steps of a failed run are re-run; this run is %(status)s.") % {"status": run.status}
            )
        try:
            services.step_plan(run, spec.steps)
        except services.StepsRefused as error:
            return Refused(str(error))
        return Ready()

    def preview(self, acquisition, spec):
        from apps.extraction import services

        if not spec.steps:
            return {}
        run = _current_run(acquisition)
        if run is None:
            return {}
        try:
            plan = services.step_plan(run, spec.steps)
        except services.StepsRefused as error:
            return {"steps": list(spec.steps), "refused": str(error)}
        return {
            "run_id": str(run.pk),
            "steps": plan["steps"],
            # The analyzer declares the steps' jobs again in its `/start`; these are the ones it replaces.
            "resend": plan["jobs"],
            "kept": plan["kept"],
            "deleted": services.step_counts(run, plan["steps"]),
            "attempt": run.attempt + 1,
            "is_active": run.is_active,
            "readers": (
                "The run is the active one: readers lose the named steps' results until the run finishes."
                if run.is_active
                else "Readers keep the acquisition's active run, if any, until this one finishes."
            ),
        }

    def task_id_for(self, acquisition, spec):
        from apps.extraction.services import task_id

        if spec.steps:
            # A fresh id (the facade's uuid4): the full run's stable id would reuse the row of the pass whose
            # steps failed, and the analyzer's delivery count is kept per task id.
            return None
        return task_id(acquisition.pk, settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION)

    def dispatch(self, dispatch):
        from apps.extraction import services
        from apps.extraction.models import ExtractionRun

        if dispatch.spec.steps:
            run = _current_run(dispatch.subject)
            if run is None or run.status != ExtractionRun.Status.FAILED:
                raise DispatchError(_("The run is no longer failed; its steps were not re-run."), status=409)
            # The run stays `failed` until the analyzer's step `/start` resets the named steps, so an analyzer
            # that ignores `steps` and starts a full pass is refused (409) and nothing is lost.
            dispatch.send(self.task, kwargs=services.step_kwargs(run, dispatch.spec.steps), queue=self.queue)
            return
        # `force`: the facade decided this generation needs a run; a done or stuck run starts over.
        if services.queue(dispatch.subject, force=True) is None:
            raise DispatchError(_("The extraction run could not be queued; see the catalog log."), status=502)

    def observe(self, acquisition):
        from apps.extraction.models import ExtractionRun

        run = _current_run(acquisition)
        if run is None:
            return None
        state = {
            ExtractionRun.Status.QUEUED: JobState.QUEUED,
            ExtractionRun.Status.RUNNING: JobState.RUNNING,
            ExtractionRun.Status.DONE: JobState.SUCCEEDED,
            ExtractionRun.Status.FAILED: JobState.FAILED,
        }[run.status]
        detail = {
            "extraction_run": {
                "id": str(run.pk),
                "status": run.status,
                "attempt": run.attempt,
                "is_active": run.is_active,
                "jobs": summarise_jobs(run.jobs),
                # #183: what a step re-run of a failed run would have to name (structured, unlike `error`).
                "missing_steps": missing_steps(run.jobs),
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }
        }
        return Observation(
            state=state, reason=run.error if state is JobState.FAILED else None, detail=detail, since=run.updated_at
        )

    def discover(self):
        return Acquisition.objects.filter(
            extraction_runs__pipeline_version=settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION
        ).select_related("entry")


class PostProcessSpec(Spec):
    #: The `POST_PROCESS_VERSION` the job is for: a job that failed for an older version is swept again.
    version: int = POST_PROCESS_VERSION


@registry.register
class PostProcessDefinition(JobDefinition):
    """Declarative (#155): the run's marker is the truth. `discover()` lists the active runs, `observe()` says
    `succeeded` when the marker is current and `queued` otherwise, so `/api/v1/jobs/summary?kind=...` is the
    backfill's progress. The sweep (`post_process_sweep`) declares the next batch."""

    kind = POST_PROCESS_KIND
    title = "Extraction post-processing"
    description = (
        "Cross-references, figure documents and vectors, section similarity and the graph projection of an active "
        "run. Swept until every active run is marked with the current version (#155)."
    )
    task = "apps.extraction.tasks.post_process_run"
    spec_model = PostProcessSpec

    @property
    def subject_model(self):
        from apps.extraction.models import ExtractionRun

        return ExtractionRun

    @property
    def max_attempts(self):
        return settings.EVILFLOWERS_POST_PROCESS_MAX_ATTEMPTS

    def scope(self, run):
        return entry_scope(run.acquisition.entry)

    def subject_from_call(self, args, kwargs):
        return uuid_arg(args, kwargs, "run_id")

    def check(self, run, spec):
        return Ready() if run.is_active else Refused(_("Only the active run of an acquisition is post-processed."))

    def args(self, dispatch):
        return [str(dispatch.subject.pk)]

    def observe(self, run):
        from apps.extraction.models import ExtractionRun
        from apps.tasks.models import Job

        job = Job.objects.filter(
            kind=self.kind, subject_type=ContentType.objects.get_for_model(ExtractionRun), subject_id=run.pk
        ).first()
        detail = {
            "post_process": {
                "marker": run.post_processed,
                "version": POST_PROCESS_VERSION,
                "last_error": job.reason if job is not None else None,
            }
        }
        if run.post_processed == POST_PROCESS_VERSION:
            return Observation(state=JobState.SUCCEEDED, detail=detail, since=run.updated_at)
        if job is not None and is_exhausted(job):
            # Failed after its attempts: stays failed (the daily `jobs-sync` must not retry it) until applied.
            return None
        # `stale` counts from when the run became active: a backlog run is declared at once.
        return Observation(state=JobState.QUEUED, detail=detail, since=run.finished_at or run.created_at)

    def discover(self):
        from apps.extraction.models import ExtractionRun

        return ExtractionRun.objects.filter(is_active=True).select_related("acquisition__entry")


# #178: Crossref metadata of a run's reference list, queued after its post-processing.
run_only(
    "extraction.enrich_bibliography",
    "apps.extraction.tasks.enrich_bibliography",
    "Reference list metadata",
    "Look the DOIs of a run's reference list items up in Crossref, at most 200 per run (#178).",
)

# #155: the beat jobs that keep post-processing and the similarity means current.
maintenance(
    "maintenance.post_process_sweep",
    "apps.extraction.tasks.post_process_sweep",
    "Post-processing sweep",
    "Declare extraction post-processing for the next active runs not marked with the current version, most read "
    "first, EVILFLOWERS_POST_PROCESS_SWEEP_BATCH in flight at most (#155).",
)
maintenance(
    "maintenance.similarity_rebuild",
    "apps.extraction.tasks.similarity_rebuild",
    "Rebuild section similarity",
    "Recompute every cross-book section pair with one set of per-language means, before the graph rebuild (#155).",
    time_limit=timedelta(seconds=SIMILARITY_REBUILD_TIME_LIMIT),
)
