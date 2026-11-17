"""IP-035: the OAI-PMH record projection (IP-033) in the console, read-only (rebuilt by `oai_sync`)."""

from apps.console.options import ConsoleModelAdmin, register
from apps.oai.models import OaiRecord


@register(OaiRecord, module="interop")
class OaiRecordAdmin(ConsoleModelAdmin):
    list_display = ("entry_id", "datestamp", "deleted", "set_specs", "fingerprint")
    list_filter = ("deleted",)
    search_fields = ("entry_id__exact",)
    ordering = ("-datestamp",)
