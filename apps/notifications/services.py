import logging

from mjml import mjml2html
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.notifications.attachments import EmailAttachment
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
    def generate_scoped_url(
        user_id: str,
        scope: str,
        resource_path: str,
        base_url: str | None = None,
    ) -> str:
        """Generate a time-limited URL using a scoped JWT appended as access_token query param.

        `base_url` defaults to `settings.EVILFLOWERS_BASE_URL`. IP-011 Phase 1
        passes an override so the reservation_available email can point at
        `EVILFLOWERS_PORTAL_URL` when set, falling back to the catalog itself.
        """
        token = JWTFactory(user_id).scoped(scope=scope)
        effective_base = (base_url if base_url is not None else settings.EVILFLOWERS_BASE_URL).rstrip("/")
        return f"{effective_base}{resource_path}?access_token={token}"

    @staticmethod
    def send(
        notification_type: str,
        recipient_user,
        context: dict,
        attachments: "list[EmailAttachment] | None" = None,
    ) -> NotificationLog:
        """Resolve recipient, render templates, compile MJML, send email, and log result.

        `attachments` are already-materialised `EmailAttachment` value objects;
        this layer stays ignorant of where they came from (see
        `apps.notifications.attachments`).
        """
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
            html_content = mjml2html(mjml_content)

            text_content = render_to_string(f"notifications/{notification_type}.txt", context)

            email = EmailMultiAlternatives(
                subject=subject,
                body=text_content,
                from_email=settings.EVILFLOWERS_NOTIFICATION_FROM_EMAIL,
                to=[recipient_email],
            )
            email.attach_alternative(html_content, "text/html")

            for attachment in attachments or []:
                email.attach(attachment.filename, attachment.content, attachment.mimetype)

            email.send()

            log.status = NotificationLog.Status.SENT
            log.sent_at = timezone.now()
        except Exception as e:
            logger.exception("Failed to send notification %s to %s", notification_type, recipient_email)
            log.status = NotificationLog.Status.FAILED
            log.error_message = str(e)

        log.save()
        return log
