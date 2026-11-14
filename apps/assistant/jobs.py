"""Assistant job kinds (IP-031): annotation highlights, section summaries, figure descriptions, their sweeps."""

from apps.core.models import Annotation, Entry
from apps.tasks.contract import Dispatch, JobDefinition, Scope, Spec, entry_scope, maintenance, registry, uuid_arg


class HighlightSpec(Spec):
    language: str | None = None


@registry.register
class HighlightAnnotationDefinition(JobDefinition[Annotation, HighlightSpec]):
    kind = "assistant.highlight_annotation"
    title = "Annotation highlights"
    description = "Run the highlight agent for a pending agent annotation and store its highlights."
    task = "apps.assistant.tasks.highlight_annotation"
    subject_model = Annotation
    spec_model = HighlightSpec

    def scope(self, annotation) -> Scope:
        acquisition = getattr(getattr(annotation, "user_acquisition", None), "acquisition", None)
        return entry_scope(getattr(acquisition, "entry", None))

    def subject_from_call(self, args, kwargs):
        return uuid_arg(args, kwargs, "annotation_id")

    def args(self, dispatch: Dispatch[Annotation, HighlightSpec]) -> list:
        return [str(dispatch.subject.pk), dispatch.spec.language]


class SummariseSpec(Spec):
    force: bool = False


@registry.register
class SummariseSectionsDefinition(JobDefinition[Entry, SummariseSpec]):
    kind = "assistant.summarise_sections"
    title = "Section summaries"
    description = "Generate the section summaries of one publication that are missing or stale (IP-030)."
    task = "apps.assistant.tasks.summarise_sections"
    subject_model = Entry
    spec_model = SummariseSpec
    #: A run lost to a rollout or killed at the time limit is sent again; it skips the sections already stored.
    max_attempts = 3

    def scope(self, entry):
        return entry_scope(entry)

    def subject_from_call(self, args, kwargs):
        return uuid_arg(args, kwargs, "entry_id")

    def args(self, dispatch: Dispatch[Entry, SummariseSpec]) -> list:
        return [str(dispatch.subject.pk), dispatch.spec.force]


class DescribeFiguresSpec(Spec):
    limit: int = 50


@registry.register
class DescribeFiguresDefinition(JobDefinition[None, DescribeFiguresSpec]):
    kind = "assistant.describe_figures"
    title = "Figure descriptions"
    description = "Describe figures without a printed caption, off-peak, a batch at a time (IP-030)."
    task = "apps.assistant.tasks.describe_figures"
    spec_model = DescribeFiguresSpec

    def kwargs(self, dispatch: Dispatch[None, DescribeFiguresSpec]) -> dict:
        return {"limit": dispatch.spec.limit}


# #154: the off-peak sweeps (beat, EVILFLOWERS_ASSISTANT_*_SCHEDULE) that declare the jobs above.
maintenance(
    "maintenance.assistant_summarise_sweep",
    "apps.assistant.tasks.summarise_sweep",
    "Section summary sweep",
    "Declare section summaries for the next publications that lack them, most read first (#154).",
)
maintenance(
    "maintenance.assistant_describe_figures_sweep",
    "apps.assistant.tasks.describe_figures_sweep",
    "Figure description sweep",
    "Declare the next batch of figure descriptions, EVILFLOWERS_ASSISTANT_DESCRIBE_FIGURES_BATCH at a time (#154).",
)
