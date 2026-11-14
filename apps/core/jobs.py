"""Core job kinds (IP-031): table of contents (per entry) and search embeddings (run-only batches)."""

from django.utils.translation import gettext as _

from apps.core.models import Acquisition, Entry
from apps.tasks.contract import (
    Dispatch,
    JobDefinition,
    Ready,
    Refused,
    Spec,
    entry_scope,
    registry,
    run_only,
    uuid_arg,
)


@registry.register
class TableOfContentsDefinition(JobDefinition[Entry, Spec]):
    kind = "core.table_of_contents"
    title = "Table of contents"
    description = "Store the outline (chapters) and page count of the entry's PDF."
    task = "apps.core.tasks.store_table_of_contents"
    subject_model = Entry

    def scope(self, entry):
        return entry_scope(entry)

    def subject_from_call(self, args, kwargs):
        return uuid_arg(args, kwargs, "entry_id")

    def check(self, entry, spec):
        if not entry.acquisitions.filter(mime=Acquisition.AcquisitionMIME.PDF).exclude(content="").exists():
            return Refused(_("The entry has no stored PDF."))
        return Ready()

    def args(self, dispatch: Dispatch[Entry, Spec]) -> list:
        return [str(dispatch.subject.pk)]


run_only(
    "core.embed_entries",
    "apps.core.tasks.embed_entries",
    "Search embeddings",
    "Embed entries through TEI for semantic search (IP-021); one run per batch of entries.",
)
