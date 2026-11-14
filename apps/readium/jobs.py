"""Readium job kinds (IP-031): LCP encryption (#148) and the license/reservation beat tasks.

`readium.encrypt` keeps its truth in `EncryptedContent`: the definition observes it, so the job of
every package (also ones nobody declared, LCP switched off since, rows older than the ledger) is
listed in `/api/v1/jobs` with the state the dashboard counts.
"""

from django.utils.translation import gettext as _

from apps.core.models import Acquisition
from apps.tasks.contract import (
    Blocked,
    Dispatch,
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
)

ENCRYPTABLE_MIMES = (Acquisition.AcquisitionMIME.PDF, Acquisition.AcquisitionMIME.EPUB)


class EncryptSpec(Spec):
    """`{}` encrypts (or re-sends a pending/failed package); `reencrypt` replaces a registered one."""

    reencrypt: bool = False
    #: With `reencrypt`: even if active loans break (their readers lose access).
    force: bool = False


def _encrypted_content(acquisition):
    from apps.readium.models import EncryptedContent

    try:
        return acquisition.encrypted_content
    except EncryptedContent.DoesNotExist:
        return None


@registry.register
class EncryptDefinition(JobDefinition[Acquisition, EncryptSpec]):
    kind = "readium.encrypt"
    title = "LCP encryption"
    description = (
        "Encrypt a PDF/EPUB acquisition with lcpencrypt and register it with the LCP server. Apply `{}` to "
        "encrypt or re-send, `{reencrypt: true}` to replace a registered package (blocked by active loans "
        "unless `force`)."
    )
    task = "evilflowers_lcpencrypt_worker.lcpencrypt"
    queue = "evilflowers_lcpencrypt_worker"
    subject_model = Acquisition
    spec_model = EncryptSpec

    @property
    def observes(self):
        from apps.readium.models import EncryptedContent

        return (EncryptedContent,)

    def scope(self, acquisition):
        return entry_scope(acquisition.entry)

    def subject_of(self, encrypted_content):
        return encrypted_content.acquisition

    def subject_from_call(self, args, kwargs):
        from apps.readium.models import EncryptedContent

        content_id = (kwargs or {}).get("contentid")
        if not content_id:
            return None
        acquisition_id = (
            EncryptedContent.objects.filter(lcp_content_id=content_id).values_list("acquisition_id", flat=True).first()
        )
        return str(acquisition_id) if acquisition_id else None

    def check(self, acquisition, spec: EncryptSpec):
        from apps.readium.models import EncryptedContent
        from apps.readium.services.content_encryption_service import ContentEncryptionService

        if acquisition.mime not in ENCRYPTABLE_MIMES:
            return Refused(_("Only PDF and EPUB files are encrypted."))
        if not acquisition.content:
            return Refused(_("The acquisition has no stored file."))
        encrypted_content = _encrypted_content(acquisition)
        status = encrypted_content.status if encrypted_content is not None else None
        resend = status in (EncryptedContent.EncryptionStatus.PENDING, EncryptedContent.EncryptionStatus.FAILED)
        # Re-sending an existing pending/failed package keeps working with LCP switched off since (as
        # `requeue_encryption` always did); a new or replaced package needs LCP on.
        if not (resend and not spec.reencrypt) and not acquisition.entry.read_config("readium_enabled"):
            return Blocked(_("Readium LCP is switched off for this publication."))
        done = status in (EncryptedContent.EncryptionStatus.COMPLETED, EncryptedContent.EncryptionStatus.REGISTERED)
        if done and not spec.reencrypt:
            return Refused(
                _("Already encrypted (%(status)s); apply {reencrypt: true} to replace it.") % {"status": status}
            )
        if spec.reencrypt and encrypted_content is not None and not spec.force:
            loans = ContentEncryptionService.active_licenses(encrypted_content).count()
            if loans:
                return Blocked(
                    _("%(count)d active loan(s) would break; waits for them to end (or apply with force).")
                    % {"count": loans}
                )
        return Ready()

    def preview(self, acquisition, spec):
        from apps.readium.services.content_encryption_service import ContentEncryptionService

        encrypted_content = _encrypted_content(acquisition)
        loans = ContentEncryptionService.active_licenses(encrypted_content).count() if encrypted_content else 0
        return {"active_loans": loans, "metadata": ContentEncryptionService.publication_metadata(acquisition.entry)}

    def dispatch(self, dispatch: Dispatch[Acquisition, EncryptSpec]) -> None:
        from apps.readium.models import EncryptedContent
        from apps.readium.services.content_encryption_service import ContentEncryptionService

        acquisition, spec = dispatch.subject, dispatch.spec
        encrypted_content = _encrypted_content(acquisition)
        # The service sends the task itself, under the run id (`current_run_id()`).
        try:
            if encrypted_content is None:
                encrypted_content = ContentEncryptionService.encrypt_acquisition(acquisition)
            elif spec.reencrypt:
                ContentEncryptionService.reencrypt(encrypted_content, force=spec.force)
            else:
                ContentEncryptionService.requeue(encrypted_content)
        except ValueError as error:
            raise DispatchError(str(error)) from error
        encrypted_content.refresh_from_db()
        if encrypted_content.status == EncryptedContent.EncryptionStatus.FAILED:
            raise DispatchError(encrypted_content.error_message or _("The source could not be prepared."))

    def observe(self, acquisition):
        from apps.readium.models import EncryptedContent
        from apps.readium.services.encryption_jobs import encryption_job, is_stuck

        encrypted_content = _encrypted_content(acquisition)
        if encrypted_content is None:
            return None
        status = EncryptedContent.EncryptionStatus
        state = {
            status.REGISTERED: JobState.SUCCEEDED,
            status.COMPLETED: JobState.RUNNING,  # waits for the LCP server's registration
            status.FAILED: JobState.FAILED,
            status.PENDING: JobState.QUEUED,
        }[encrypted_content.status]
        detail = {"encryption": {**encryption_job(encrypted_content), "stuck": is_stuck(encrypted_content)}}
        reason = encrypted_content.error_message if state is JobState.FAILED else None
        # `stale` then counts from the last dispatch, as the dashboard's `encryption_stuck` does.
        since = (
            encrypted_content.last_dispatched_at or encrypted_content.created_at
            if state is JobState.QUEUED
            else encrypted_content.updated_at
        )
        return Observation(state=state, reason=reason, detail=detail, since=since)

    def discover(self):
        return Acquisition.objects.filter(encrypted_content__isnull=False).select_related("entry", "encrypted_content")


maintenance(
    "readium.sweep_unclaimed_reservations",
    "apps.readium.tasks.sweep_unclaimed_reservations",
    "Expire unclaimed reservations",
    "Expire `available` reservations past their claim deadline and promote the next reader.",
)
maintenance(
    "readium.notify_expiring_licenses",
    "apps.readium.tasks.notify_expiring_licenses",
    "Notify expiring loans",
    "Send `license_expiring_soon` for loans expiring within the reminder window.",
)
maintenance(
    "readium.expire_lapsed_licenses",
    "apps.readium.tasks.expire_lapsed_licenses",
    "Expire lapsed loans",
    "Move naturally expired loans out of ready/active.",
)
maintenance(
    "readium.reservation_claim_reminder_sweep",
    "apps.readium.tasks.reservation_claim_reminder_sweep",
    "Remind unclaimed reservations",
    "Remind readers whose claim window is about to close.",
)
