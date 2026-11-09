"""IP-025: notification branding and LDAP sync over MCP — superusers only."""

import os
import shutil
import tempfile
from unittest.mock import patch

from django.core import mail
from django.test import override_settings

from apps.mcp.tests.test_management import ManagementTestCase
from apps.notifications.models import NotificationBranding, NotificationContact

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@override_settings(
    EVILFLOWERS_BASE_URL="https://elvira.stuba.sk",
    EVILFLOWERS_NOTIFICATION_FROM_EMAIL="elvira@stuba.sk",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class NotificationToolTests(ManagementTestCase):
    def setUp(self):
        super().setUp()
        self.volume = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.volume)
        os.makedirs(os.path.join(self.volume, "assets"))
        with open(os.path.join(self.volume, "assets", "stu-logo.png"), "wb") as handle:
            handle.write(PNG)
        overrides = override_settings(EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR=self.volume)
        overrides.enable()
        self.addCleanup(overrides.disable)

    def test_read_tools_refuse_everyone_but_superusers(self):
        for user in (self.reader, self.manager):
            for name in ("get_notification_branding", "preview_notification"):
                with self.subTest(user=user.username, tool=name):
                    self.assertIn("restricted to administrators", self.refused(name, {}, user))

    def test_get_lists_assets_and_types(self):
        payload = self.ok("get_notification_branding", {}, self.superuser)
        self.assertEqual(payload["assets"], ["stu-logo.png"])
        self.assertIn("license_created", payload["notification_types"])
        self.assertEqual(payload["stored"], {})

    def test_stu_branding_end_to_end(self):
        payload = self.ok(
            "update_notification_branding",
            {
                "library_name": "Digitálna knižnica Elvíra",
                "logo": "stu-logo.png",
                "header_background": "#FFFFFF",
                "header_text_color": "#333333",
                "primary_color": "#a60c2f",
                "footer_text": "Slovenská technická univerzita v Bratislave",
            },
            self.superuser,
        )
        self.assertEqual(payload["effective"]["header_background"], "#ffffff")
        self.assertEqual(
            payload["effective"]["logo_url"], "https://elvira.stuba.sk/notifications/v1/assets/stu-logo.png"
        )
        self.assertEqual(NotificationBranding.objects.get().updated_by, self.superuser)

        preview = self.ok("preview_notification", {"notification_type": "reservation_available"}, self.superuser)
        self.assertIn("stu-logo.png", preview["html"])
        self.assertIn("#a60c2f", preview["html"])
        self.assertIn("Slovenská technická univerzita", preview["text"])

        reset = self.ok("update_notification_branding", {"reset": ["logo"]}, self.superuser)
        self.assertEqual(reset["changed"], ["logo"])
        self.assertEqual(reset["effective"]["logo_url"], "")

    def test_invalid_tokens_are_refused_together(self):
        message = self.refused(
            "update_notification_branding",
            {"primary_color": "red", "logo": "missing.png", "website_url": "elvira.stuba.sk"},
            self.superuser,
        )
        for field in ("primary_color", "logo", "website_url"):
            self.assertIn(field, message)
        self.assertIn("stu-logo.png", message)  # tells the caller what is available
        self.assertFalse(NotificationBranding.objects.exists())

    def test_send_test_goes_only_to_the_caller(self):
        self.assertIn(
            "no primary e-mail contact",
            self.refused("send_test_notification", {}, self.superuser),
        )
        NotificationContact.objects.create(user=self.superuser, type="email", value="root@stuba.sk", is_primary=True)
        self.ok("update_notification_branding", {"from_name": "Elvíra STU"}, self.superuser)

        payload = self.ok("send_test_notification", {"notification_type": "license_expiring_soon"}, self.superuser)

        self.assertEqual(payload["status"], "sent")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["root@stuba.sk"])
        # A non-ASCII display name survives Django's message build as an encoded header.
        from email.header import decode_header, make_header

        sender = str(make_header(decode_header(mail.outbox[0].message()["From"])))
        self.assertEqual(sender, "Elvíra STU <elvira@stuba.sk>")

    def test_header_injection_in_from_name_is_refused(self):
        message = self.refused("update_notification_branding", {"from_name": "Elvíra\nBcc: x@y"}, self.superuser)
        self.assertIn("single line", message)

    def test_sync_ldap_profiles_reports_without_addresses(self):
        from apps.core.models import AuthSource, User
        from apps.notifications.tests.test_directory_contacts import CONFIG, directory, ldap_attrs

        source = AuthSource.objects.create(name="stuba.sk", driver=AuthSource.Driver.LDAP, content=CONFIG)
        User.objects.create(username="xnovakova", auth_source=source)
        connection = directory({"xnovakova": ldap_attrs("alena@stuba.sk")})

        with patch("apps.core.services.ldap_directory.ldap_initialize", return_value=connection):
            payload = self.ok("sync_ldap_profiles", {"dry_run": True}, self.superuser)

        self.assertTrue(payload["dry_run"])
        [result] = payload["sources"]
        self.assertEqual(result["summary"]["changed"], 1)
        self.assertNotIn("alena@stuba.sk", str(payload))
        self.assertFalse(NotificationContact.objects.exists())
