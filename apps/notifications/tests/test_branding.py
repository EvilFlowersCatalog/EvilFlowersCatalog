"""IP-025: notification branding — tokens in the database over settings, assets on the IaC volume."""

import os
import shutil
import tempfile

from django.test import TestCase, override_settings

from apps.notifications import branding
from apps.notifications.models import NotificationBranding
from apps.notifications.registry import TemplateRegistry
from apps.notifications.services import NotificationService

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
STU = {
    "library_name": "Digitálna knižnica Elvíra",
    "logo": "stu-logo.png",
    "header_background": "#ffffff",
    "header_text_color": "#333333",
    "primary_color": "#a60c2f",
    "footer_text": "Slovenská technická univerzita v Bratislave\nVazovova 5, 812 43 Bratislava",
    "website_url": "https://elvira.stuba.sk",
    "from_name": "Elvíra STU",
}


class OverridesVolumeMixin:
    def setUp(self):
        super().setUp()
        self.volume = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.volume)
        os.makedirs(os.path.join(self.volume, "assets"))
        os.makedirs(os.path.join(self.volume, "templates", "notifications"))
        for name, content in (("stu-logo.png", PNG), ("logo.svg", b"<svg/>"), ("notes.txt", b"x")):
            with open(os.path.join(self.volume, "assets", name), "wb") as handle:
                handle.write(content)
        overrides = override_settings(
            EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR=self.volume,
            EVILFLOWERS_BASE_URL="https://elvira.stuba.sk/",
            EVILFLOWERS_NOTIFICATION_LIBRARY_NAME="Settings Library",
            EVILFLOWERS_NOTIFICATION_FROM_EMAIL="elvira@stuba.sk",
        )
        overrides.enable()
        self.addCleanup(overrides.disable)


class BrandingResolutionTests(OverridesVolumeMixin, TestCase):
    def test_without_a_row_settings_and_defaults_apply(self):
        brand = branding.resolve()
        self.assertEqual(brand.library_name, "Settings Library")
        self.assertEqual(brand.primary_color, branding.DEFAULTS["primary_color"])
        self.assertEqual(brand.logo_url, "")

    def test_stored_tokens_win_and_empty_tokens_inherit(self):
        NotificationBranding.objects.create(primary_color="#a60c2f", library_name="")
        brand = branding.resolve()
        self.assertEqual(brand.primary_color, "#a60c2f")
        self.assertEqual(brand.library_name, "Settings Library")

    def test_asset_logo_becomes_a_public_absolute_url(self):
        NotificationBranding.objects.create(logo="stu-logo.png")
        self.assertEqual(branding.resolve().logo_url, "https://elvira.stuba.sk/notifications/v1/assets/stu-logo.png")

    def test_missing_asset_falls_back_to_text_header(self):
        NotificationBranding.objects.create(logo="gone.png")
        self.assertEqual(branding.resolve().logo_url, "")

    def test_only_mail_renderable_images_are_assets(self):
        self.assertEqual(branding.list_assets(), ["stu-logo.png"])
        self.assertIsNone(branding.asset_path("../assets/stu-logo.png"))
        self.assertIsNone(branding.asset_path("logo.svg"))

    def test_validation(self):
        self.assertEqual(branding.validate_color("#A60C2F"), "#a60c2f")
        for bad in ("red", "#12345", "#a60c2f;x", '#a60c2f" onload="'):
            with self.assertRaises(ValueError):
                branding.validate_color(bad)
        self.assertEqual(branding.validate_logo("stu-logo.png"), "stu-logo.png")
        self.assertEqual(branding.validate_logo("https://x.example/l.png"), "https://x.example/l.png")
        for bad in ("http://x.example/l.png", "https://x.example/l.svg", "missing.png", "logo.svg"):
            with self.assertRaises(ValueError):
                branding.validate_logo(bad)


class BrandedRenderingTests(OverridesVolumeMixin, TestCase):
    def setUp(self):
        super().setUp()
        NotificationBranding.objects.create(**STU)

    def test_html_carries_logo_colours_and_footer(self):
        rendered = NotificationService.render("license_created", TemplateRegistry.sample_context())
        self.assertIn('src="https://elvira.stuba.sk/notifications/v1/assets/stu-logo.png"', rendered.html)
        self.assertIn('alt="Digitálna knižnica Elvíra"', rendered.html)
        self.assertIn("#a60c2f", rendered.html)
        self.assertIn("Vazovova 5", rendered.html)
        self.assertIn("https://elvira.stuba.sk", rendered.html)
        self.assertNotIn("#4A90D9", rendered.html)

    def test_text_footer_is_not_html_escaped(self):
        row = NotificationBranding.current()
        row.footer_text = "Knižnica & informačné centrum"
        row.save()
        rendered = NotificationService.render("license_created", TemplateRegistry.sample_context())
        self.assertIn("Knižnica & informačné centrum", rendered.text)
        self.assertIn("do not reply", rendered.text)
        self.assertIn("Knižnica &amp; informačné centrum", rendered.html)

    def test_from_name(self):
        from email.header import decode_header, make_header

        address = NotificationService.from_address()
        self.assertTrue(address.endswith("<elvira@stuba.sk>"))
        self.assertIn("Elvíra STU", str(make_header(decode_header(address))))

    def test_every_notification_type_renders_with_branding(self):
        context = TemplateRegistry.sample_context()
        for notification_type in TemplateRegistry.get_template_names():
            with self.subTest(notification_type=notification_type):
                rendered = NotificationService.render(notification_type, context)
                self.assertTrue(rendered.subject)
                self.assertIn("stu-logo.png", rendered.html)
                self.assertIn("Vazovova 5", rendered.text)

    def test_volume_template_override_wins(self):
        from django.conf import settings

        subjects = os.path.join(self.volume, "templates", "notifications", "subjects")
        os.makedirs(subjects)
        with open(os.path.join(subjects, "license_created.txt"), "w") as handle:
            handle.write("Nová výpožička: {{ entry_title }}")

        # What settings/base.py does at start-up when the variable is set.
        engine = {
            **settings.TEMPLATES[0],
            "DIRS": [os.path.join(self.volume, "templates"), *settings.TEMPLATES[0]["DIRS"]],
        }
        with override_settings(TEMPLATES=[engine]):
            rendered = NotificationService.render("license_created", TemplateRegistry.sample_context())

        self.assertTrue(rendered.subject.startswith("Nová výpožička:"))
        self.assertIn("stu-logo.png", rendered.html)  # the rest still comes from the built-in templates
        self.assertIn(os.path.join("subjects", "license_created.txt"), branding.list_template_overrides())


class TemplateValidationTests(TestCase):
    def test_base_compiles_with_an_empty_context(self):
        # `compile_templates` renders every template with {} — the tokens need defaults.
        self.assertEqual(TemplateRegistry.validate_templates(), {})


@override_settings(EVILFLOWERS_BASE_URL="https://elvira.stuba.sk")
class AssetEndpointTests(OverridesVolumeMixin, TestCase):
    def test_serves_png_publicly_with_cache_headers(self):
        response = self.client.get("/notifications/v1/assets/stu-logo.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("max-age", response["Cache-Control"])
        self.assertEqual(b"".join(response.streaming_content), PNG)

    def test_refuses_anything_else(self):
        for path in ("logo.svg", "notes.txt", "missing.png", "..%2Fassets%2Fstu-logo.png", ".hidden.png"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(f"/notifications/v1/assets/{path}").status_code, 404)

    @override_settings(EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR="")
    def test_404_without_a_volume(self):
        self.assertEqual(self.client.get("/notifications/v1/assets/stu-logo.png").status_code, 404)
