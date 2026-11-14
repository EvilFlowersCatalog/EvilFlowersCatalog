"""Assistant job kinds (IP-031): annotation highlights, section summaries, figure descriptions."""

from apps.core.models import Annotation, Entry
from apps.tasks.kinds import JobKind, SystemKind, entry_scope, first_arg, registry


@registry.register
class HighlightAnnotationKind(JobKind):
    name = "assistant.highlight_annotation"
    title = "Annotation highlights"
    description = "Run the highlight agent for a pending agent annotation and store its highlights."
    task = "apps.assistant.tasks.highlight_annotation"
    subject_model = Annotation
    options_schema = {
        "type": "object",
        "properties": {"language": {"type": ["string", "null"]}},
        "additionalProperties": False,
    }

    def subject_from_call(self, args, kwargs):
        return first_arg(args, kwargs, "annotation_id")

    def scope(self, annotation):
        user_acquisition = getattr(annotation, "user_acquisition", None)
        acquisition = getattr(user_acquisition, "acquisition", None)
        return entry_scope(getattr(acquisition, "entry", None))

    def args(self, job, annotation, options):
        return [str(annotation.pk), options.get("language")]

    def retry_options(self, job):
        args = job.job_args or []
        return {"language": args[1] if len(args) > 1 else (job.job_kwargs or {}).get("language")}


@registry.register
class SummariseSectionsKind(JobKind):
    name = "assistant.summarise_sections"
    title = "Section summaries"
    description = "Generate the section summaries of one publication that are missing or stale (IP-030)."
    task = "apps.assistant.tasks.summarise_sections"
    subject_model = Entry
    options_schema = {
        "type": "object",
        "properties": {"force": {"type": "boolean", "description": "Regenerate every summary."}},
        "additionalProperties": False,
    }

    def subject_from_call(self, args, kwargs):
        return first_arg(args, kwargs, "entry_id")

    def scope(self, entry):
        return entry_scope(entry)

    def args(self, job, entry, options):
        return [str(entry.pk), bool(options.get("force"))]


@registry.register
class DescribeFiguresKind(SystemKind):
    name = "assistant.describe_figures"
    title = "Figure descriptions"
    description = "Describe figures without a printed caption, off-peak, a batch at a time (IP-030)."
    task = "apps.assistant.tasks.describe_figures"
    options_schema = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 1000}},
        "additionalProperties": False,
    }

    def kwargs(self, job, subject, options):
        return {"limit": int(options.get("limit") or 50)}
