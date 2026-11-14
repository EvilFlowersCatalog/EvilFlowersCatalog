"""Assistant job kinds (IP-031): annotation highlights, section summaries, figure descriptions."""

from apps.core.models import Annotation, Entry
from apps.tasks.contract import Dispatch, JobDefinition, Scope, Spec, entry_scope, registry, uuid_arg


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
