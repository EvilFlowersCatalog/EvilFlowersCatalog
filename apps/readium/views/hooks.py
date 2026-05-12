"""
Webhook Views for Readium LCP Integration

Handles callbacks from external services:
- lcpencrypt CMS notification (POST with CMSMsg format)
"""

import json
import logging

from django.http import JsonResponse
from django.views import View

from apps.readium.services import ContentEncryptionService

logger = logging.getLogger(__name__)


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

    If this webhook returns non-2xx, lcpencrypt rolls back by deleting
    the content from the LCP server. So we MUST return 2xx on success.
    """

    def post(self, request, *args, **kwargs):
        """Handle POST CMS notification from lcpencrypt."""
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

        # Check if EncryptedContent exists
        encrypted_content = ContentEncryptionService.get_by_lcp_content_id(lcp_content_id)
        if not encrypted_content:
            logger.error(f"EncryptedContent not found for contentid: {lcp_content_id}")
            return JsonResponse({"status": "error", "message": "EncryptedContent not found"}, status=404)

        try:
            ContentEncryptionService.mark_encryption_completed(lcp_content_id)
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