"""
Webhook Views for Readium LCP Integration

Handles callbacks from external services:
- lcpencrypt CMS notification (POST with CMSMsg format)
- lcpencrypt worker result report (POST, one per task, success or failure)
"""

import base64
import json
import logging
import secrets
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.http import JsonResponse
from django.views import View

from apps.readium.services import ContentEncryptionService
from evil_flowers_catalog import telemetry

logger = logging.getLogger(__name__)


def _expected_authorization(setting: str = "EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL") -> str | None:
    """Basic auth header the caller sends, derived from the credentials in a callback URL.

    lcpencrypt (and the worker, for the report URL) turns
    `http://user:pass@host/...` into a Basic Authorization header. Mirrors
    lcpencrypt's `getUsernamePassword`: credentials count only when the URL
    carries both a username and a password. Returns None otherwise, in which
    case the webhook stays open (network isolation is the only guard).
    """
    url = urlsplit(getattr(settings, setting, None) or "")
    if not url.username or url.password is None:
        return None
    credentials = f"{unquote(url.username)}:{unquote(url.password)}"
    return "Basic " + base64.b64encode(credentials.encode()).decode()


def _authorized(request, setting: str) -> bool:
    expected = _expected_authorization(setting)
    provided = request.headers.get("Authorization", "").encode("latin-1", "replace")
    return not expected or secrets.compare_digest(provided, expected.encode())


class EncryptionWebhook(View):
    """
    Webhook endpoint for lcpencrypt CMS notification.

    lcpencrypt sends a POST after successful encryption with the CMSMsg format:
    {
        "uuid": "lcp_content_id",
        "title": "Publication title",
        "content_type": "application/pdf+lcp",
        "date_published": "...",
        "description": "...",
        ...
    }

    If this webhook returns non-2xx (or is unreachable within 15 s),
    lcpencrypt rolls back by deleting the content from the LCP server — yet
    still exits 0, so the worker reports success. A credential mismatch here
    therefore silently un-registers the publication.
    """

    def post(self, request, *args, **kwargs):
        """Handle POST CMS notification from lcpencrypt."""
        if not _authorized(request, "EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL"):
            logger.warning("Rejected encryption webhook request with missing or invalid credentials")
            return JsonResponse({"status": "error", "message": "Unauthorized"}, status=401)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            logger.error("Invalid JSON in encryption webhook request")
            return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)

        logger.info(f"Encryption webhook received: {payload}")

        # lcpencrypt sends "uuid" (CMSMsg format), not "contentid"
        lcp_content_id = payload.get("uuid") or payload.get("contentid")

        if not lcp_content_id:
            logger.error("Missing uuid/contentid in encryption webhook payload")
            return JsonResponse({"status": "error", "message": "Missing uuid"}, status=400)

        # lcpencrypt sends no `traceparent`, so this request starts its own
        # trace; the attribute is what joins it to the dispatch (issue #99).
        telemetry.set_span_attributes(lcp__content_id=lcp_content_id)

        # Check if EncryptedContent exists
        encrypted_content = ContentEncryptionService.get_by_lcp_content_id(lcp_content_id)
        if not encrypted_content:
            logger.error(f"EncryptedContent not found for contentid: {lcp_content_id}")
            return JsonResponse({"status": "error", "message": "EncryptedContent not found"}, status=404)

        try:
            # `mark_encryption_completed` re-fetches the row and sets `encrypted_at`.
            # Use the returned fresh instance — the local `encrypted_content` above
            # was loaded before the timestamp was written and would clobber it
            # back to NULL on the next save (breaking `is_ready_for_licensing`).
            encrypted_content = ContentEncryptionService.mark_encryption_completed(lcp_content_id)
            logger.info(f"Marked encryption completed for lcp_content_id: {lcp_content_id}")

            # lcpencrypt already registered with LCP server before calling this webhook
            ContentEncryptionService.mark_registered_with_lcp_server(encrypted_content)

            return JsonResponse(
                {
                    "status": "success",
                    "lcp_content_id": lcp_content_id,
                    "message": "Encryption completed and registered",
                }
            )

        except Exception as e:
            logger.exception(f"Error marking encryption completed for {lcp_content_id}: {str(e)}")
            return JsonResponse({"status": "error", "message": str(e)}, status=500)


class EncryptionResultWebhook(View):
    """
    Result report from the lcpencrypt worker, one per task, for every outcome (issue #99).

    The worker POSTs here when the task was dispatched with a `report` URL
    (`EVILFLOWERS_READIUM_LCPENCRYPT_REPORT_URL`):
    {
        "contentid": "<lcp_content_id>",
        "task_id": "<celery task id>",
        "trace_id": "<hex>",
        "success": false,
        "error": "Encryption failed: …",
        "report": {"cover": "rendered", "pages": 237, ...}
    }

    - `success: false` marks the job FAILED with the worker's error. That
      includes a package lcpencrypt withdrew from the License Server after a
      failed notify, even if the notify hook had already marked it REGISTERED.
    - `success: true` only stores the report; the notify webhook owns the
      REGISTERED flip and may come before or after this request.
    - A report for a task other than the row's current one (the job was
      re-queued meanwhile) is acknowledged with 200 and ignored.

    Response codes follow the worker's retry rules: 400/401/404 are final, a
    5xx is retried. The request carries the worker's `traceparent`, so this
    span joins the dispatch trace.
    """

    def post(self, request, *args, **kwargs):
        if not _authorized(request, "EVILFLOWERS_READIUM_LCPENCRYPT_REPORT_URL"):
            logger.warning("Rejected encryption result report with missing or invalid credentials")
            return JsonResponse({"status": "error", "message": "Unauthorized"}, status=401)

        try:
            result = self._parse(request.body)
        except ValueError as error:
            return JsonResponse({"status": "error", "message": str(error)}, status=400)

        lcp_content_id, task_id = result["contentid"], result["task_id"]
        telemetry.set_span_attributes(
            lcp__content_id=lcp_content_id, lcp__task_id=task_id, lcp__success=result["success"]
        )

        if not ContentEncryptionService.get_by_lcp_content_id(lcp_content_id):
            logger.error("Encryption result for unknown contentid %s", lcp_content_id)
            return JsonResponse({"status": "error", "message": "EncryptedContent not found"}, status=404)

        try:
            recorded = self._record(result)
        except Exception as e:
            logger.exception("Error recording encryption result for %s", lcp_content_id)
            return JsonResponse({"status": "error", "message": str(e)}, status=500)

        status = "recorded" if recorded else "ignored"
        if recorded and not result["success"]:
            logger.warning("Encryption failed for %s (task %s): %s", lcp_content_id, task_id, result["error"])
        else:
            logger.info(
                "Encryption result for %s (task %s, success=%s): %s",
                lcp_content_id,
                task_id,
                result["success"],
                status,
            )
        return JsonResponse({"status": status, "lcp_content_id": lcp_content_id, "task_id": task_id})

    @staticmethod
    def _parse(body: bytes) -> dict:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON")
        if not isinstance(payload, dict) or not payload.get("contentid"):
            raise ValueError("Missing contentid")
        if not isinstance(payload.get("success"), bool):
            raise ValueError("Missing or non-boolean success")
        report = payload.get("report")
        return {
            "contentid": str(payload["contentid"]),
            "task_id": payload.get("task_id") or None,
            "trace_id": payload.get("trace_id") or None,
            "success": payload["success"],
            "error": payload.get("error") or "The encryption worker reported a failure without a reason",
            "report": report if isinstance(report, dict) else None,
        }

    @staticmethod
    def _record(result: dict) -> bool:
        """False when the report is about an older task than the row's current one."""
        if result["success"]:
            return ContentEncryptionService.record_encryption_report(
                result["contentid"], result["task_id"], result["trace_id"], result["report"]
            )
        return ContentEncryptionService.mark_encryption_failed(
            result["contentid"], result["error"], task_id=result["task_id"], report=result["report"]
        )
