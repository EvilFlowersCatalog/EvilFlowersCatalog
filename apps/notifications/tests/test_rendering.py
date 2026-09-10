"""
Notification rendering after the August 2026 review round.

The library asked for its own name in the header/footer, readable dates
instead of raw ISO-8601 strings, and a "do not reply" footer.
"""

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings

from apps.notifications.services import NotificationService
from apps.notifications.templatetags.notification_extras import isodate, isodatetime


class IsoDateFilterTests(SimpleTestCase):
    def test_iso_string_is_humanised(self):
        rendered = isodatetime("2026-09-10T06:49:49.487565+00:00")
        self.assertNotIn("T06:49", rendered)
        self.assertNotIn(".487565", rendered)
        self.assertIn("2026", rendered)

    def test_date_variant(self):
        self.assertNotIn(":", isodate("2026-09-10T06:49:49+00:00"))

    def test_garbage_and_empty_pass_through(self):
        self.assertEqual(isodatetime("not a date"), "not a date")
        self.assertEqual(isodatetime(""), "")
        self.assertEqual(isodatetime(None), "")

    def test_usable_from_templates(self):
        out = Template("{% load notification_extras %}{{ v|isodatetime }}").render(
            Context({"v": "2026-09-10T06:49:49+00:00"})
        )
        self.assertIn("2026", out)


@override_settings(
    EVILFLOWERS_NOTIFICATION_LIBRARY_NAME="Digitálna knižnica Elvíra / Digital Library Elvira",
    EVILFLOWERS_CONTACT_EMAIL="elvira@stuba.sk",
    EVILFLOWERS_PORTAL_URL="https://elvira.stuba.sk/",
)
class RenderContextTests(SimpleTestCase):
    def test_deployment_values_are_injected_and_event_context_wins(self):
        ctx = NotificationService.render_context({"user_name": "Alena", "portal_url": "override"})
        self.assertEqual(ctx["library_name"], "Digitálna knižnica Elvíra / Digital Library Elvira")
        self.assertEqual(ctx["contact_email"], "elvira@stuba.sk")
        self.assertEqual(ctx["portal_url"], "override")
        self.assertEqual(ctx["user_name"], "Alena")

    def test_templates_use_library_name_not_product_name(self):
        from django.template.loader import render_to_string

        ctx = NotificationService.render_context(
            {
                "user_name": "Alena",
                "entry_title": "Book",
                "entry_author": "Author",
                "starts_at": "2026-08-20T00:00:00+02:00",
                "expires_at": "2026-08-27T00:00:00+02:00",
                "download_url": "https://x/y.lcpl?token=t",
                "download_expires_hours": 72,
                "passphrase_hint": "",
            }
        )
        for name in ("notifications/license_created.txt", "notifications/license_created.mjml"):
            out = render_to_string(name, ctx)
            self.assertIn("Digital Library Elvira", out)
            self.assertNotIn("Evil Flowers Catalog", out)
            self.assertNotIn("2026-08-27T00:00:00", out)
            self.assertIn("do not reply", out)
