"""#20: instance settings — the registry, the DB → Django setting → default chain, the cache and REST."""

from django.core import mail
from django.test import TestCase, override_settings

from apps.core import settings_store
from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, Setting, User
from apps.notifications.services import NotificationService


class SettingsFixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.admin = User.objects.create(username="admin", name="A", surname="D", auth_source=source, is_superuser=True)
        cls.reader = User.objects.create(username="reader", name="R", surname="D", auth_source=source)
        from apps.content import policies

        policies.ensure_presets()

    def setUp(self):
        settings_store.invalidate()
        self.addCleanup(settings_store.invalidate)

    def headers(self, user) -> dict:
        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}


class SettingsStoreTests(SettingsFixtures):
    @override_settings(EVILFLOWERS_CONTACT_EMAIL="elvira@stuba.sk")
    def test_a_key_resolves_stored_then_django_setting_then_default(self):
        self.assertEqual(
            settings_store.resolve("branding.contact_email"), {"value": "elvira@stuba.sk", "source": "setting"}
        )
        settings_store.update("branding.contact_email", "kniznica@stuba.sk", user=self.admin)
        self.assertEqual(settings_store.get("branding.contact_email"), "kniznica@stuba.sk")
        self.assertEqual(Setting.objects.get(pk="branding.contact_email").updated_by, self.admin)
        settings_store.update("branding.contact_email", None, user=self.admin)
        self.assertEqual(settings_store.resolve("branding.contact_email")["source"], "setting")
        self.assertEqual(settings_store.resolve("content.default_policy"), {"value": "parity", "source": "default"})

    def test_a_write_is_validated_against_the_key_schema(self):
        with self.assertRaises(settings_store.SettingError) as refused:
            settings_store.update("content.external_processor_default", "cloud")
        self.assertEqual(refused.exception.schema_path, "enum")
        with self.assertRaises(settings_store.SettingError):
            settings_store.update("branding.contact_email", "not an address")
        with self.assertRaises(settings_store.SettingError) as unknown:
            settings_store.update("nope.key", 1)
        self.assertIn("Unknown setting", unknown.exception.message)

    def test_the_default_policy_must_name_a_policy(self):
        with self.assertRaises(settings_store.SettingError):
            settings_store.update("content.default_policy", "no-such-policy")
        self.assertTrue(settings_store.update("content.default_policy", "open-access")["changed"])

    def test_a_dry_run_writes_nothing(self):
        result = settings_store.update("content.external_processor_default", "local", dry_run=True)
        self.assertEqual((result["changed"], result["value"], result["source"]), (True, "local", "stored"))
        self.assertFalse(Setting.objects.exists())
        self.assertEqual(settings_store.get("content.external_processor_default"), "external")

    def test_values_are_cached_and_a_write_drops_the_cache(self):
        settings_store.get("branding.logo")
        with self.assertNumQueries(0):
            settings_store.get("branding.logo")
        settings_store.update("branding.logo", "data:image/png;base64," + "A" * 200_000)
        self.assertEqual(len(settings_store.get("branding.logo")), len("data:image/png;base64,") + 200_000)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_the_contact_address_reaches_notifications(self):
        settings_store.update("branding.contact_email", "kniznica@stuba.sk")
        self.assertEqual(NotificationService.render_context({})["contact_email"], "kniznica@stuba.sk")
        self.assertEqual(len(mail.outbox), 0)


class SettingsApiTests(SettingsFixtures):
    def test_only_superusers_read_and_write(self):
        self.assertEqual(self.client.get("/api/v1/settings", headers=self.headers(self.reader)).status_code, 403)
        response = self.client.put(
            "/api/v1/settings/content.default_policy",
            data={"value": "open-access"},
            content_type="application/json",
            headers=self.headers(self.reader),
        )
        self.assertEqual(response.status_code, 403)

    def test_the_registry_lists_every_key_with_its_schema_and_source(self):
        items = self.client.get("/api/v1/settings", headers=self.headers(self.admin)).json()["response"]
        keys = {item["key"]: item for item in items}
        self.assertTrue(
            {
                "content.default_policy",
                "content.cite_base_url",
                "content.external_processor_default",
                "branding.contact_email",
                "branding.logo",
            }
            <= set(keys)
        )
        self.assertEqual(keys["content.default_policy"]["value"], "parity")
        self.assertEqual(keys["content.default_policy"]["source"], "default")
        self.assertEqual(keys["content.external_processor_default"]["schema"]["enum"], ["local", "external"])

    def test_put_validates_resets_and_dry_runs(self):
        url = "/api/v1/settings/content.external_processor_default"
        refused = self.client.put(
            url, {"value": "cloud"}, content_type="application/json", headers=self.headers(self.admin)
        )
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.json()["additional_data"]["schema_path"], "enum")

        dry = self.client.put(
            url + "?dry_run=true",
            {"value": "local"},
            content_type="application/json",
            headers=self.headers(self.admin),
        ).json()["response"]
        self.assertTrue(dry["dry_run"])
        self.assertFalse(Setting.objects.exists())

        stored = self.client.put(
            url, {"value": "local"}, content_type="application/json", headers=self.headers(self.admin)
        )
        self.assertEqual(stored.json()["response"]["source"], "stored")
        reset = self.client.put(
            url, {"value": None}, content_type="application/json", headers=self.headers(self.admin)
        )
        self.assertEqual(reset.json()["response"]["source"], "default")

    def test_an_unknown_key_is_not_found(self):
        response = self.client.get("/api/v1/settings/nope.key", headers=self.headers(self.admin))
        self.assertEqual(response.status_code, 404)

    def test_api_keys_carry_a_processor_only_admins_may_declare_local(self):
        created = self.client.post(
            "/api/v1/api_keys",
            {"name": "claude", "processor": "external"},
            content_type="application/json",
            headers=self.headers(self.reader),
        )
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["response"]["processor"], "external")
        refused = self.client.post(
            "/api/v1/api_keys",
            {"name": "ollama", "processor": "local"},
            content_type="application/json",
            headers=self.headers(self.reader),
        )
        self.assertEqual(refused.status_code, 403)
        plain = self.client.post(
            "/api/v1/api_keys", {"name": "plain"}, content_type="application/json", headers=self.headers(self.reader)
        )
        self.assertIsNone(plain.json()["response"]["processor"])
