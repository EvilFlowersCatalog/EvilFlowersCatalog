"""Core job kinds (IP-031): table of contents and search embeddings."""

from django.utils.translation import gettext as _

from apps.core.models import Entry
from apps.tasks.kinds import JobKind, SystemKind, entry_scope, first_arg, registry


@registry.register
class TableOfContentsKind(JobKind):
    name = "core.table_of_contents"
    title = "Table of contents"
    description = "Store the outline (chapters) and page count of the entry's PDF."
    task = "apps.core.tasks.store_table_of_contents"
    subject_model = Entry

    def subject_from_call(self, args, kwargs):
        return first_arg(args, kwargs, "entry_id")

    def scope(self, entry):
        return entry_scope(entry)

    def eligible(self, entry, options):
        from apps.core.models import Acquisition

        if not entry.acquisitions.filter(mime=Acquisition.AcquisitionMIME.PDF).exclude(content="").exists():
            return _("The entry has no stored PDF.")
        return None

    def args(self, job, entry, options):
        return [str(entry.pk)]


@registry.register
class EmbedEntriesKind(SystemKind):
    """Batches of entries; the subject is the batch, so the kind has none (`entry_ids` in options)."""

    name = "core.embed_entries"
    title = "Search embeddings"
    description = "Embed entries through TEI for semantic search (IP-021). Without entry_ids: nothing to do."
    task = "apps.core.tasks.embed_entries"
    exclusive = False
    options_schema = {
        "type": "object",
        "properties": {"entry_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}}},
        "required": ["entry_ids"],
        "additionalProperties": False,
    }

    def eligible(self, subject, options):
        if not options.get("entry_ids"):
            return _("Pass entry_ids to embed.")
        return None

    def args(self, job, subject, options):
        return [sorted(str(entry_id) for entry_id in options["entry_ids"])]

    def retry_options(self, job):
        ids = (job.job_args or [[]])[0] if job.job_args else (job.options or {}).get("entry_ids", [])
        return {"entry_ids": list(ids or [])}
