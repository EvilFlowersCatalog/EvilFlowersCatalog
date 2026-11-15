"""Keep content grants (#175) in step with licences and own copies. Every licence state write goes through
`save()` (the Status Server sync, the expiry sweep, device registration, return and revoke), so `post_save` sees
each transition."""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.content import grants


@receiver(post_save, sender="readium.License", dispatch_uid="content.grant_license")
def _grant_license(sender, instance, **kwargs):
    grants.record_license(instance)


@receiver(post_save, sender="core.UserAcquisition", dispatch_uid="content.grant_acquisition")
def _grant_acquisition(sender, instance, **kwargs):
    grants.record_acquisition(instance)


@receiver(post_delete, sender="core.UserAcquisition", dispatch_uid="content.revoke_acquisition")
def _revoke_acquisition(sender, instance, **kwargs):
    grants.revoke_acquisition(instance)


@receiver(post_save, sender="core.Entry", dispatch_uid="content.tdmrep_entry")
@receiver(post_delete, sender="core.Entry", dispatch_uid="content.tdmrep_entry_deleted")
def _tdmrep_entry(sender, instance, **kwargs):
    """A rights edit (REST, MCP), a reservation lifted or a deleted publication changes `/.well-known/tdmrep.json`
    (#177). One cache delete; the file is rebuilt on the next read."""
    from apps.content import tdmrep

    tdmrep.invalidate()
