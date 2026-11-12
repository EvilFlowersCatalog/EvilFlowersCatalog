import logging
from dataclasses import dataclass
from email.utils import formataddr

from mjml import mjml2html
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.notifications import branding
from apps.notifications.attachments import EmailAttachment
from apps.notifications.branding import Branding
from apps.notifications.models import NotificationContact, NotificationLog
from evil_flowers_catalog import tracing

logger = logging.getLogger(__name__)


def _deliver(email: EmailMultiAlternatives) -> None:
    """`email.send()` as a CLIENT span: the SMTP relay is a node of its own in the service graph (IP-029).

    Only the SMTP backend talks to a server; the console and locmem backends get the span without
    `server.address`, so tests and local runs show the step but no phantom relay.
    """
    smtp = settings.EMAIL_BACKEND.endswith("smtp.EmailBackend")
    with tracing.client_span(
        "smtp send",
        system="smtp",
        server_address=settings.EMAIL_HOST if smtp else None,
        server_port=settings.EMAIL_PORT if smtp else None,
        attributes={
            "evilflowers.email.backend": settings.EMAIL_BACKEND.rsplit(".", 2)[-2],
            "evilflowers.email.attachments": len(email.attachments),
        },
    ):
        email.send()


@dataclass(frozen=True)
class RenderedNotification:
    subject: str
    text: str
    html: str


class NotificationService:
    @staticmethod
    def resolve_recipient_email(user) -> str | None:
        """Resolve primary email contact for user. Returns None if no contact exists."""
        contact = NotificationContact.objects.filter(
            user=user, type=NotificationContact.ContactType.EMAIL, is_primary=True
        ).first()
        return contact.value if contact else None

    @staticmethod
    @transaction.atomic
    def set_contact(user, contact_type: str, value: str, is_primary: bool = True) -> NotificationContact:
        """Upsert a notification contact and keep at most one primary per (user, type).

        `resolve_recipient_email` picks the primary contact, so demoting the
        previous primary here is what makes the newly saved address the one
        notifications actually go to.
        """
        contact, _ = NotificationContact.objects.update_or_create(
            user=user,
            type=contact_type,
            value=value,
            defaults={"is_primary": is_primary},
        )
        if is_primary:
            NotificationContact.objects.filter(user=user, type=contact_type, is_primary=True).exclude(
                pk=contact.pk
            ).update(is_primary=False)
        return contact

    @staticmethod
    @transaction.atomic
    def sync_directory_contact(user, contact_type: str, value: str, *, dry_run: bool = False):
        """Make the directory's address a contact of `user` without overriding the user's own choice.

        - The address already exists as a row → it is marked as directory-owned
          (`adopted`) — that is how rows imported before IP-025, which carry
          the default `user` source, are taken over.
        - Otherwise an earlier directory-owned row is rewritten to the new
          address (`updated`, the directory changed), or a new one is created.
        - It becomes primary when the user has no primary contact of the type
          or the current primary is itself directory-owned (`promoted`). A
          primary the user entered themselves is never demoted.

        The previous login code did `update_or_create(user=, type=)`, which
        raised `MultipleObjectsReturned` — and so broke the login — as soon as
        a user had added a second address through the API.
        """
        from apps.core.services.ldap_directory import ContactChange

        ldap_source = NotificationContact.Source.LDAP
        contacts = NotificationContact.objects.select_for_update().filter(user=user, type=contact_type)
        current_primary = contacts.filter(is_primary=True).first()
        should_be_primary = current_primary is None or current_primary.source == ldap_source

        # The importer lower-cases; a legacy row may not be (`Name.Surname@stuba.sk`).
        contact = contacts.filter(value=value).first() or contacts.filter(value__iexact=value).first()
        if contact is not None:
            if contact.source != ldap_source or contact.value != value:
                action = "adopted"
            elif should_be_primary and not contact.is_primary:
                action = "promoted"
            else:
                action = "unchanged"
        else:
            contact = contacts.filter(source=ldap_source).order_by("-is_primary", "-updated_at").first()
            action = "updated" if contact is not None else "created"

        is_primary = should_be_primary or (contact is not None and contact.is_primary)

        if dry_run:
            return ContactChange(contact_type=contact_type, action=action, is_primary=is_primary)

        if contact is None:
            contact = NotificationContact(user=user, type=contact_type)
        if action != "unchanged":
            contact.value = value
            contact.source = ldap_source
            contact.is_primary = is_primary
            contact.save()
            # Stale directory rows (an address the directory dropped) go away;
            # rows the user entered stay.
            contacts.filter(source=ldap_source).exclude(pk=contact.pk).delete()
            if is_primary:
                contacts.filter(is_primary=True).exclude(pk=contact.pk).update(is_primary=False)

        return ContactChange(contact_type=contact_type, action=action, is_primary=is_primary)

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
    def render_context(context: dict, brand: "Branding | None" = None) -> dict:
        """Per-notification context plus the deployment-wide values every template needs.

        `library_name` is the header/footer brand (STU: "Digitálna knižnica
        Elvíra / Digital Library Elvira"), `contact_email` the address readers
        are told to write to, `portal_url` the reader-facing site when one is
        configured, `brand` the IP-025 branding tokens (logo, colours, footer).
        Kept out of `context_snapshot` — they are configuration, not event data.
        """
        brand = brand if brand is not None else branding.resolve()
        return {
            "library_name": brand.library_name,
            "contact_email": settings.EVILFLOWERS_CONTACT_EMAIL,
            "portal_url": (getattr(settings, "EVILFLOWERS_PORTAL_URL", "") or "").rstrip("/"),
            "brand": brand.as_context(),
            **context,
        }

    @staticmethod
    def render(notification_type: str, context: dict, brand: "Branding | None" = None) -> RenderedNotification:
        """Subject, plain text and HTML of one notification — no recipient, no log, nothing sent."""
        render_context = NotificationService.render_context(context, brand)
        subject = " ".join(render_to_string(f"notifications/subjects/{notification_type}.txt", render_context).split())
        html = mjml2html(render_to_string(f"notifications/{notification_type}.mjml", render_context))
        text = render_to_string(f"notifications/{notification_type}.txt", render_context)
        return RenderedNotification(subject=subject, text=text, html=html)

    @staticmethod
    def from_address(brand: "Branding | None" = None) -> str:
        brand = brand if brand is not None else branding.resolve()
        address = settings.EVILFLOWERS_NOTIFICATION_FROM_EMAIL
        return formataddr((brand.from_name, address)) if brand.from_name else address

    @staticmethod
    @tracing.traced(
        "notification.send",
        lambda notification_type, *args, **kwargs: {"evilflowers.notification.type": notification_type},
    )
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
            tracing.set_attributes({"evilflowers.notification.status": NotificationLog.Status.NO_CONTACT})
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
            brand = branding.resolve()
            rendered = NotificationService.render(notification_type, context, brand)
            log.subject = rendered.subject

            email = EmailMultiAlternatives(
                subject=rendered.subject,
                body=rendered.text,
                from_email=NotificationService.from_address(brand),
                to=[recipient_email],
            )
            email.attach_alternative(rendered.html, "text/html")

            for attachment in attachments or []:
                email.attach(attachment.filename, attachment.content, attachment.mimetype)

            _deliver(email)

            log.status = NotificationLog.Status.SENT
            log.sent_at = timezone.now()
        except Exception as e:
            logger.exception("Failed to send notification %s to %s", notification_type, recipient_email)
            log.status = NotificationLog.Status.FAILED
            log.error_message = str(e)

        log.save()
        tracing.set_attributes(
            {"evilflowers.notification.status": log.status, "evilflowers.notification.log_id": log.pk}
        )
        return log
