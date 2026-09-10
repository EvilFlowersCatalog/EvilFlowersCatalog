"""
Notification contact self-service API tests.

Users manage their own notification contacts through
`/api/v1/notification-contacts` — the delivery layer
(`NotificationService.resolve_recipient_email`) only ever reads the primary
contact, so `set_contact` must keep at most one primary per (user, type).
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase


class NotificationContactFormTests(SimpleTestCase):
    def test_rejects_invalid_email_value(self):
        from apps.api.forms.notification_contacts import NotificationContactForm

        form = NotificationContactForm({"type": "email", "value": "not-an-email", "is_primary": True})
        self.assertFalse(form.is_valid())
        self.assertIn("valid email", str(form.errors))

    def test_accepts_valid_email_value(self):
        from apps.api.forms.notification_contacts import NotificationContactForm

        form = NotificationContactForm({"type": "email", "value": "student@stuba.sk"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["value"], "student@stuba.sk")


class SetContactPrimaryInvariantTests(SimpleTestCase):
    """`set_contact` upserts and demotes previous primaries of the same type.

    The tests call the `__wrapped__` inner function to bypass the
    `transaction.atomic` decorator — SimpleTestCase forbids DB connections.
    """

    @staticmethod
    def _set_contact(*args, **kwargs):
        from apps.notifications.services import NotificationService

        return NotificationService.set_contact.__wrapped__(*args, **kwargs)

    @patch("apps.notifications.services.NotificationContact")
    def test_primary_demotes_siblings(self, contact_model):
        user = MagicMock()
        contact = MagicMock()
        contact_model.objects.update_or_create.return_value = (contact, True)

        result = self._set_contact(user, "email", "student@stuba.sk", is_primary=True)

        self.assertIs(result, contact)
        contact_model.objects.update_or_create.assert_called_once_with(
            user=user,
            type="email",
            value="student@stuba.sk",
            defaults={"is_primary": True},
        )
        contact_model.objects.filter.assert_called_once_with(user=user, type="email", is_primary=True)
        contact_model.objects.filter.return_value.exclude.assert_called_once_with(pk=contact.pk)
        contact_model.objects.filter.return_value.exclude.return_value.update.assert_called_once_with(is_primary=False)

    @patch("apps.notifications.services.NotificationContact")
    def test_non_primary_leaves_siblings_alone(self, contact_model):
        contact_model.objects.update_or_create.return_value = (MagicMock(), True)

        self._set_contact(MagicMock(), "email", "backup@stuba.sk", is_primary=False)

        contact_model.objects.filter.assert_not_called()


class NotificationContactUrlTests(SimpleTestCase):
    def test_routes_registered(self):
        from uuid import uuid4

        from django.urls import reverse

        self.assertTrue(reverse("api:notification-contact-management").endswith("/notification-contacts"))
        detail = reverse("api:notification-contact-detail", kwargs={"contact_id": uuid4()})
        self.assertIn("/notification-contacts/", detail)
