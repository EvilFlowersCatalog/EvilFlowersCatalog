import logging

import mrml
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.notifications.models import NotificationContact, NotificationLog

logger = logging.getLogger(__name__)


class NotificationService:
    @staticmethod
    def resolve_recipient_email(user) -> str | None:
        """Resolve primary email contact for user. Returns None if no contact exists."""
        contact = NotificationContact.objects.filter(
            user=user, type=NotificationContact.ContactType.EMAIL, is_primary=True
        ).first()
        return contact.value if contact else None

    @staticmethod
    def generate_scoped_url(user_id: str, scope: str, resource_path: str) -> str:
        """Generate a time-limited URL using a scoped JWT appended as access_token query param."""
        token = JWTFactory(user_id).scoped(scope=scope)
        base_url = settings.EVILFLOWERS_BASE_URL.rstrip("/")
        return f"{base_url}{resource_path}?access_token={token}"

    @staticmethod
    def send(notification_type: str, recipient_user, context: dict) -> NotificationLog:
        """Resolve recipient, render templates, compile MJML, send email, and log result."""
        recipient_email = NotificationService.resolve_recipient_email(recipient_user)
        if not recipient_email:
            return NotificationLog.objects.create(
                recipient=recipient_user,
                recipient_email="",
                notification_type=notification_type,
                status=NotificationLog.Status.NO_CONTACT,
                context_snapshot=context,
            )

        log = NotificationLog.objects.create(
            recipient=recipient_user,
            recipient_email=recipient_email,
            notification_type=notification_type,
            status=NotificationLog.Status.QUEUED,
            context_snapshot=context,
        )

        try:
            subject = render_to_string(f"notifications/subjects/{notification_type}.txt", context).strip()
            log.subject = subject

            mjml_content = render_to_string(f"notifications/{notification_type}.mjml", context)
            html_content = mrml.to_html(mjml_content).content

            text_content = render_to_string(f"notifications/{notification_type}.txt", context)

            email = EmailMultiAlternatives(
                subject=subject,
                body=text_content,
                from_email=settings.EVILFLOWERS_NOTIFICATION_FROM_EMAIL,
                to=[recipient_email],
            )
            email.attach_alternative(html_content, "text/html")
            email.send()

            log.status = NotificationLog.Status.SENT
            log.sent_at = timezone.now()
        except Exception as e:
            logger.exception("Failed to send notification %s to %s", notification_type, recipient_email)
            log.status = NotificationLog.Status.FAILED
            log.error_message = str(e)

        log.save()
        return log
