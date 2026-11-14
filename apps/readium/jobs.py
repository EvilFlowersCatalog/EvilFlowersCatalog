"""Readium job kinds (IP-031): LCP encryption (#148) and the license/reservation beat tasks."""

from django.utils.translation import gettext as _

from apps.core.models import Acquisition
from apps.tasks.kinds import JobAction, JobKind, JobRefused, SystemKind, entry_scope, registry

ENCRYPTABLE_MIMES = (Acquisition.AcquisitionMIME.PDF, Acquisition.AcquisitionMIME.EPUB)


def _encrypted_content(acquisition):
    from apps.readium.models import EncryptedContent

    try:
        return acquisition.encrypted_content
    except EncryptedContent.DoesNotExist:
        return None


def _requeue(job, acquisition, options, user):
    """Send a stuck (`pending`) or `failed` job again: same content id, new task (MCP `requeue_encryption`)."""
    from apps.tasks.services import JobService

    new = JobService.start(EncryptKind.name, acquisition.pk, user=user, force=True, retry_of=job)
    return {"job_id": str(new.pk), "status": new.status}


def _reencrypt(job, acquisition, options, user):
    """Replace a registered package (MCP `reencrypt_publication`); `dry_run` reports and sends nothing."""
    from apps.readium.services.content_encryption_service import ContentEncryptionService
    from apps.tasks.services import JobService

    encrypted_content = _encrypted_content(acquisition)
    if encrypted_content is None:
        raise JobRefused(_("This publication was never encrypted; start an encryption job instead."), status=422)
    force = bool(options.get("force"))
    active_loans = ContentEncryptionService.active_licenses(encrypted_content).count()
    if options.get("dry_run"):
        return {
            "dry_run": True,
            "active_loans": active_loans,
            "would_requeue": force or not active_loans,
            "metadata": ContentEncryptionService.publication_metadata(acquisition.entry),
        }
    if active_loans and not force:
        raise JobRefused(
            _("%(count)d active loan(s) would break; their readers lose access. Pass force to re-encrypt anyway.")
            % {"count": active_loans}
        )
    new = JobService.start(
        EncryptKind.name,
        acquisition.pk,
        user=user,
        force=True,
        retry_of=job,
        options={"reencrypt": True, "force": force},
    )
    return {"job_id": str(new.pk), "status": new.status, "active_loans": active_loans}


@registry.register
class EncryptKind(JobKind):
    name = "readium.encrypt"
    title = "LCP encryption"
    description = (
        "Encrypt a PDF/EPUB acquisition with lcpencrypt and register it with the LCP server. "
        "The publication cannot be lent until the job's EncryptedContent is `registered`."
    )
    task = "evilflowers_lcpencrypt_worker.lcpencrypt"
    queue = "evilflowers_lcpencrypt_worker"
    subject_model = Acquisition
    options_schema = {
        "type": "object",
        "properties": {
            "reencrypt": {"type": "boolean", "description": "Replace an already registered package."},
            "force": {"type": "boolean", "description": "With reencrypt: even with active loans."},
        },
        "additionalProperties": False,
    }
    actions = {
        "requeue": JobAction(
            name="requeue",
            title="Re-queue",
            description="Send a stuck or failed encryption again under the same content id.",
            handler=_requeue,
        ),
        "reencrypt": JobAction(
            name="reencrypt",
            title="Re-encrypt",
            description=(
                "Encrypt a registered publication again (same content id). Loans issued before it stop "
                "opening once refreshed; refused with active loans unless force. dry_run reports first."
            ),
            handler=_reencrypt,
            options_schema={
                "type": "object",
                "properties": {"force": {"type": "boolean"}, "dry_run": {"type": "boolean"}},
                "additionalProperties": False,
            },
        ),
    }

    def subject_from_call(self, args, kwargs):
        from apps.readium.models import EncryptedContent

        content_id = (kwargs or {}).get("contentid")
        if not content_id:
            return None
        acquisition_id = (
            EncryptedContent.objects.filter(lcp_content_id=content_id).values_list("acquisition_id", flat=True).first()
        )
        return str(acquisition_id) if acquisition_id else None

    def scope(self, acquisition):
        return entry_scope(acquisition.entry)

    def eligible(self, acquisition, options):
        from apps.readium.models import EncryptedContent

        if not acquisition.entry.read_config("readium_enabled"):
            return _("Readium LCP is not enabled for this publication.")
        if acquisition.mime not in ENCRYPTABLE_MIMES:
            return _("Only PDF and EPUB files are encrypted.")
        if not acquisition.content:
            return _("The acquisition has no stored file.")
        encrypted_content = _encrypted_content(acquisition)
        done = (EncryptedContent.EncryptionStatus.COMPLETED, EncryptedContent.EncryptionStatus.REGISTERED)
        if encrypted_content is not None and encrypted_content.status in done and not options.get("reencrypt"):
            return _("Already encrypted (%(status)s); use the reencrypt action.") % {
                "status": encrypted_content.status
            }
        return None

    def dispatch(self, job, acquisition, options):
        from apps.readium.models import EncryptedContent
        from apps.readium.services.content_encryption_service import ContentEncryptionService

        encrypted_content = _encrypted_content(acquisition)
        try:
            if encrypted_content is None:
                encrypted_content = ContentEncryptionService.encrypt_acquisition(acquisition)
            elif options.get("reencrypt"):
                ContentEncryptionService.reencrypt(encrypted_content, force=bool(options.get("force")))
            else:
                ContentEncryptionService.requeue(encrypted_content)
        except ValueError as error:
            raise JobRefused(str(error), status=422)
        encrypted_content.refresh_from_db()
        # `_queue_encryption_task` turns the row failed when it cannot prepare the source (license page);
        # nothing was sent then.
        if encrypted_content.status == EncryptedContent.EncryptionStatus.FAILED:
            raise JobRefused(encrypted_content.error_message or _("The source could not be prepared."), status=422)

    def detail(self, job, acquisition):
        from apps.readium.services.encryption_jobs import encryption_job

        encrypted_content = _encrypted_content(acquisition) if acquisition is not None else None
        if encrypted_content is None:
            return {}
        view = encryption_job(encrypted_content)
        # The LCP row describes its last dispatch; say whether that is this job.
        view["current"] = encrypted_content.task_id == str(job.pk)
        return {"encryption": view}


@registry.register
class SweepUnclaimedReservationsKind(SystemKind):
    name = "readium.sweep_unclaimed_reservations"
    title = "Expire unclaimed reservations"
    description = "Expire `available` reservations past their claim deadline and promote the next reader."
    task = "apps.readium.tasks.sweep_unclaimed_reservations"


@registry.register
class NotifyExpiringLicensesKind(SystemKind):
    name = "readium.notify_expiring_licenses"
    title = "Notify expiring loans"
    description = "Send `license_expiring_soon` for loans expiring within the reminder window."
    task = "apps.readium.tasks.notify_expiring_licenses"


@registry.register
class ExpireLapsedLicensesKind(SystemKind):
    name = "readium.expire_lapsed_licenses"
    title = "Expire lapsed loans"
    description = "Move naturally expired loans out of ready/active."
    task = "apps.readium.tasks.expire_lapsed_licenses"


@registry.register
class ReservationClaimReminderKind(SystemKind):
    name = "readium.reservation_claim_reminder_sweep"
    title = "Remind unclaimed reservations"
    description = "Remind readers whose claim window is about to close."
    task = "apps.readium.tasks.reservation_claim_reminder_sweep"
