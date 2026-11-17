"""IP-035: Readium LCP in the console (Lending module), read-only.

Licences and encryption jobs change state through the LCP/LSD servers and the encryption webhook; an
edited row here would disagree with the License Server. Re-queue and re-encrypt stay REST/MCP operations
(`requeue_encryption`, `reencrypt_publication`), which talk to the servers.
"""

from django.utils.translation import gettext_lazy as _

from apps.console.options import ConsoleModelAdmin, register
from apps.readium.models import EncryptedContent, License, Reservation


@register(EncryptedContent, module="lending")
class EncryptedContentAdmin(ConsoleModelAdmin):
    list_display = ("acquisition", "status", "attempts", "encrypted_at", "registered_at", "last_dispatched_at")
    list_filter = ("status",)
    search_fields = ("acquisition__entry__title", "lcp_content_id", "task_id")
    list_select_related = ("acquisition__entry",)
    attention = (
        (_("encryptions failed"), {"status": EncryptedContent.EncryptionStatus.FAILED}),
        (_("encryptions pending"), {"status": EncryptedContent.EncryptionStatus.PENDING}),
    )


@register(License, module="lending")
class LicenseAdmin(ConsoleModelAdmin):
    list_display = ("entry", "user", "state", "starts_at", "expires_at", "device_count", "renewal_count")
    list_filter = ("state",)
    search_fields = ("entry__title", "user__username", "lcp_license_id")
    list_select_related = ("entry", "user")
    exclude = ("passphrase_hash",)
    date_hierarchy = "starts_at"

    def record(self, request, obj):
        """A loan: the book (with its cover), the reader, how long it still runs, its devices and renewals."""
        from django.utils import timezone

        from apps.console.records import Badge, Stat, change_url
        from apps.core.console_records import LICENSE_TONES, thumbnail_url

        record = super().record(request, obj)
        record.image = thumbnail_url(obj.entry)
        record.title = obj.entry.title
        record.subtitle = _("lent to %(user)s") % {"user": obj.user}
        record.eyebrow = f"{_('Loan')} · {obj.entry.catalog.title}"
        record.badges.append(Badge(obj.get_state_display(), LICENSE_TONES.get(obj.state, "muted")))
        days = (obj.expires_at - timezone.now()).days if obj.expires_at else None
        record.stats = [
            Stat(_("Days left"), days if days is not None and days >= 0 else "—", tone="accent"),
            Stat(_("Devices"), obj.device_count),
            Stat(_("Renewals"), obj.renewal_count),
            Stat(_("Started"), obj.starts_at.date() if obj.starts_at else "—"),
            Stat(_("Expires"), obj.expires_at.date() if obj.expires_at else "—"),
        ]
        record.facts[1:1] = [
            (_("Publication"), str(obj.entry)),
            (_("Reader"), str(obj.user)),
            (_("LCP licence"), str(obj.lcp_license_id or "—")),
        ]
        for label, target in ((_("Publication"), obj.entry), (_("Reader"), obj.user)):
            url = change_url(target)
            if url:
                from apps.console.records import Action

                record.actions.insert(0, Action(label, url, icon="book" if target is obj.entry else "users"))
        return record


@register(Reservation, module="lending")
class ReservationAdmin(ConsoleModelAdmin):
    list_display = ("entry", "user", "position", "status", "requested_at", "available_at", "claim_deadline")
    list_filter = ("status",)
    search_fields = ("entry__title", "user__username")
    list_select_related = ("entry", "user")
