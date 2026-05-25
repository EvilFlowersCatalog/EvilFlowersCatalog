"""
Encryption Management Views

Handles manual encryption triggering and status checking for Readium LCP.
"""

import logging
from http import HTTPStatus
from uuid import UUID

from django.db import transaction
from django.utils.translation import gettext as _

from apps import openapi
from apps.api.response import SingleResponse
from apps.core.errors import ProblemDetailException, ValidationException, DetailType
from apps.core.models import Entry, Acquisition
from apps.core.views import SecuredView
from apps.files.storage import get_storage
from apps.readium.forms import EncryptionTriggerForm
from apps.readium.services import ContentEncryptionService

logger = logging.getLogger(__name__)


class EntryEncryptionView(SecuredView):
    """Manage encryption for entry content."""

    @openapi.metadata(
        description="Get encryption status for an entry's acquisition.",
        tags=["Readium", "Encryption"],
        summary="Get encryption status",
    )
    def get(self, request, entry_id: UUID):
        try:
            entry = Entry.objects.get(pk=entry_id)
        except Entry.DoesNotExist:
            raise ProblemDetailException(
                _("Entry not found"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=DetailType.NOT_FOUND,
            )

        if not entry.read_config("readium_enabled"):
            return SingleResponse(
                request,
                data={
                    "readium_enabled": False,
                    "message": "Readium is not enabled for this entry",
                },
            )

        acquisition = entry.acquisitions.filter(
            mime__in=[
                Acquisition.AcquisitionMIME.EPUB,
                Acquisition.AcquisitionMIME.PDF,
            ]
        ).first()

        if not acquisition:
            return SingleResponse(
                request,
                data={
                    "readium_enabled": True,
                    "has_acquisition": False,
                    "message": "No suitable acquisition found",
                },
            )

        if hasattr(acquisition, "encrypted_content"):
            ec = acquisition.encrypted_content
            return SingleResponse(
                request,
                data={
                    "readium_enabled": True,
                    "has_acquisition": True,
                    "encryption": {
                        "status": ec.status,
                        "lcp_content_id": ec.lcp_content_id,
                        "ready_for_licensing": ec.status == "registered",
                        "encrypted_at": ec.encrypted_at.isoformat() if ec.encrypted_at else None,
                        "registered_at": ec.registered_at.isoformat() if ec.registered_at else None,
                        "error_message": ec.error_message,
                    },
                },
            )

        return SingleResponse(
            request,
            data={
                "readium_enabled": True,
                "has_acquisition": True,
                "encryption": None,
                "message": "Encryption not started",
            },
        )

    @openapi.metadata(
        description="""
        Trigger encryption for an entry's acquisition.

        Use this to:
        - Start encryption if it hasn't been triggered
        - Re-trigger encryption after a failure (with force=true)

        Requires admin permissions.
        """,
        tags=["Readium", "Encryption"],
        summary="Trigger encryption",
    )
    def post(self, request, entry_id: UUID):
        # Require admin permission for manual encryption trigger
        if not request.user.has_perm("core.change_entry"):
            raise ProblemDetailException(
                _("Insufficient permissions"),
                status=HTTPStatus.FORBIDDEN,
            )

        try:
            entry = Entry.objects.get(pk=entry_id)
        except Entry.DoesNotExist:
            raise ProblemDetailException(
                _("Entry not found"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=DetailType.NOT_FOUND,
            )

        if not entry.read_config("readium_enabled"):
            raise ProblemDetailException(
                _("Readium is not enabled for this entry"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )

        acquisition = entry.acquisitions.filter(
            mime__in=[
                Acquisition.AcquisitionMIME.EPUB,
                Acquisition.AcquisitionMIME.PDF,
            ]
        ).first()

        if not acquisition:
            raise ProblemDetailException(
                _("No suitable acquisition found for encryption"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )

        form = EncryptionTriggerForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        force = form.cleaned_data.get("force", False)

        # Check if already encrypted
        if hasattr(acquisition, "encrypted_content") and not force:
            ec = acquisition.encrypted_content
            if ec.status in ["completed", "registered"]:
                raise ProblemDetailException(
                    _("Encryption already in progress or completed. Use force=true to re-trigger."),
                    status=HTTPStatus.CONFLICT,
                    detail_type=DetailType.CONFLICT,
                )
            # If failed, allow re-trigger without force
            if ec.status == "failed":
                ec.delete()

        # IP-008 Phase 3 C8: force re-encryption must also delete the
        # encrypted file on disk/S3. Previously only the DB row was
        # removed; the underlying blob stayed forever and storage grew
        # unbounded on every force=True trigger.
        if force and hasattr(acquisition, "encrypted_content"):
            ec_to_delete = acquisition.encrypted_content
            encrypted_path = ec_to_delete.encrypted_path
            with transaction.atomic():
                ec_to_delete.delete()

                def _drop_blob(path=encrypted_path):
                    storage = get_storage()
                    try:
                        if path and storage.exists(path):
                            storage.delete(path)
                    except Exception:
                        # Best-effort: if blob deletion fails the DB row
                        # is already gone. Log and move on so the next
                        # encryption pass can proceed.
                        logger.exception("Failed to delete encrypted blob at %s", path)

                # Schedule the blob delete only after the row delete commits.
                transaction.on_commit(_drop_blob)

        try:
            encrypted_content = ContentEncryptionService.encrypt_acquisition(acquisition)
            return SingleResponse(
                request,
                data={
                    "message": "Encryption triggered successfully",
                    "lcp_content_id": encrypted_content.lcp_content_id,
                    "status": encrypted_content.status,
                },
                status=HTTPStatus.ACCEPTED,
            )

        except ValueError as e:
            raise ProblemDetailException(
                str(e),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )
