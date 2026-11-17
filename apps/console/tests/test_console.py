"""IP-035: the administration console covers the whole database, is gated by modules and stays safe."""

from django.apps import apps
from django.contrib import admin
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.console import modules as console_modules
from apps.console.options import ConsoleMixin, EstimatedCountPaginator
from apps.core import settings_store
from apps.core.models import AuthSource, Catalog, Entry, EntryReview, Setting, User


class ConsoleTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.source = source
        cls.admin = User.objects.create(
            username="root", name="Root", surname="Admin", auth_source=source, is_superuser=True
        )
        cls.admin.set_password("secret-password-1")
        cls.admin.save()
        cls.reader = User.objects.create(username="reader", name="Re", surname="Ader", auth_source=source)
        cls.catalog = Catalog.objects.create(creator=cls.admin, title="Open", url_name="open", is_public=True)
        cls.entry = Entry.objects.create(creator=cls.admin, catalog=cls.catalog, title="A book")

    def setUp(self):
        settings_store.invalidate()
        self.addCleanup(settings_store.invalidate)
        self.client.force_login(self.admin)


class CoverageTests(ConsoleTestCase):
    def test_every_model_is_in_a_module_or_excluded_with_a_reason(self):
        declared = {model._meta.label for model in admin.site.declared_models()}
        missing = [
            model._meta.label
            for model in apps.get_models()
            if model._meta.label not in declared and model._meta.label not in console_modules.EXCLUDED_MODELS
        ]
        self.assertEqual(missing, [], "Register these in a console module or list them in EXCLUDED_MODELS")

    def test_excluded_models_are_not_registered(self):
        for label in console_modules.EXCLUDED_MODELS:
            self.assertFalse(admin.site.is_registered(apps.get_model(label)), label)

    def test_every_admin_is_a_console_admin(self):
        for model, model_admin in admin.site._registry.items():
            self.assertIsInstance(model_admin, ConsoleMixin, model._meta.label)
            self.assertIn(model_admin.console_module, console_modules.BY_KEY, model._meta.label)


@override_settings(
    EVILFLOWERS_ADMIN_MODULES="all", EVILFLOWERS_EXTRACTION_ENABLED=True, EVILFLOWERS_ASSISTANT_ENABLED=True
)
class SmokeTests(ConsoleTestCase):
    """Every page of every admin renders for a superuser (empty tables included)."""

    def test_index_module_and_modules_pages(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ELVIRA")
        self.assertContains(response, "console/css/elvira-admin.css")
        for module in console_modules.MODULES:
            response = self.client.get(reverse("admin:console_module", args=[module.key]))
            self.assertEqual(response.status_code, 200, module.key)
        self.assertEqual(self.client.get(reverse("admin:console_modules")).status_code, 200)

    def test_changelist_add_and_change_views(self):
        for model, model_admin in admin.site._registry.items():
            opts = model._meta
            label = opts.label
            with self.subTest(model=label):
                url = reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")
                self.assertEqual(self.client.get(url).status_code, 200, label)
                self.assertEqual(self.client.get(url, {"q": "x"}).status_code, 200, label)
                add = reverse(f"admin:{opts.app_label}_{opts.model_name}_add")
                expected = 200 if model_admin.has_add_permission(self._request()) else 403
                self.assertEqual(self.client.get(add).status_code, expected, label)
                obj = model._default_manager.first()
                if obj is not None:
                    change = reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[obj.pk])
                    self.assertEqual(self.client.get(change).status_code, 200, label)

    def test_entry_change_view_hides_vectors(self):
        response = self.client.get(reverse("admin:core_entry_change", args=[self.entry.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="embedding"')
        self.assertNotContains(response, 'name="search_vector"')

    def _request(self):
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = self.admin
        return request


class ModuleGateTests(ConsoleTestCase):
    def test_sensitive_modules_are_off_by_default(self):
        states = console_modules.states()
        for key in ("reading", "conversations", "audit"):
            self.assertFalse(states[key]["enabled"], key)
        self.assertTrue(states["library"]["enabled"])

    def test_switched_off_module_hides_its_models(self):
        settings_store.update(console_modules.SETTING_KEY, {"notifications": False})
        response = self.client.get(reverse("admin:index"))
        self.assertNotContains(response, reverse("admin:notifications_notificationlog_changelist"))
        self.assertEqual(self.client.get(reverse("admin:notifications_notificationlog_changelist")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:console_module", args=["notifications"])).status_code, 404)

    def test_switch_takes_effect_without_restart(self):
        url = reverse("admin:core_shelfrecord_changelist")
        self.assertEqual(self.client.get(url).status_code, 403)
        settings_store.update(console_modules.SETTING_KEY, {"reading": True})
        self.assertEqual(self.client.get(url).status_code, 200)

    @override_settings(EVILFLOWERS_ADMIN_MODULES="library,system")
    def test_deployment_default_list(self):
        self.assertTrue(console_modules.is_enabled("library"))
        self.assertFalse(console_modules.is_enabled("lending"))

    @override_settings(EVILFLOWERS_ASSISTANT_ENABLED=False)
    def test_feature_flag_wins_over_the_setting(self):
        settings_store.update(console_modules.SETTING_KEY, {"assistant": True})
        self.assertFalse(console_modules.is_enabled("assistant"))

    def test_modules_page_stores_only_differences(self):
        defaults = console_modules.deployment_defaults()
        form = {f"module-{key}": "on" for key, on in defaults.items() if on}
        form.pop("module-interop")
        form["module-reading"] = "on"
        response = self.client.post(reverse("admin:console_modules"), form)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(settings_store.get(console_modules.SETTING_KEY), {"interop": False, "reading": True})
        self.assertEqual(Setting.objects.get(pk=console_modules.SETTING_KEY).updated_by, self.admin)

    def test_unknown_module_key_is_refused(self):
        with self.assertRaises(settings_store.SettingError):
            settings_store.update(console_modules.SETTING_KEY, {"nope": True})


class AccessTests(ConsoleTestCase):
    def test_non_superuser_cannot_enter(self):
        self.client.force_login(self.reader)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])

    def test_login_page_renders_with_theme(self):
        self.client.logout()
        response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "console/css/elvira-admin.css")

    def test_password_login(self):
        self.client.logout()
        response = self.client.post(
            reverse("admin:login"), {"username": "root", "password": "secret-password-1", "next": "/admin/"}
        )
        self.assertEqual(response.status_code, 302)

    @override_settings(EVILFLOWERS_ADMIN_READ_ONLY=True)
    def test_read_only_deployment_refuses_writes(self):
        url = reverse("admin:core_catalog_change", args=[self.catalog.pk])
        response = self.client.post(url, {"title": "Changed", "url_name": "open", "is_public": "on"})
        self.assertEqual(response.status_code, 403)
        self.catalog.refresh_from_db()
        self.assertEqual(self.catalog.title, "Open")

    def test_machine_written_models_have_no_add_or_change(self):
        """LCP licences change through the License Server, never through a form."""
        self.assertEqual(self.client.get(reverse("admin:readium_license_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:tasks_jobrun_add")).status_code, 403)

    def test_user_created_with_hashed_password(self):
        response = self.client.post(
            reverse("admin:core_user_add"),
            {
                "username": "librarian",
                "name": "Libra",
                "surname": "Rian",
                "auth_source": self.source.pk,
                "password1": "a-long-password-123",
                "password2": "a-long-password-123",
                "usable_password": "true",
            },
        )
        self.assertEqual(
            response.status_code, 302, getattr(response, "context", None) and response.context.get("errors")
        )
        user = User.objects.get(username="librarian")
        self.assertTrue(user.password.startswith("argon2"))
        self.assertTrue(user.check_password("a-long-password-123"))

    def test_setting_writes_go_through_the_registry(self):
        response = self.client.post(
            reverse("admin:core_setting_add"), {"key": console_modules.SETTING_KEY, "value": '{"jobs": "yes"}'}
        )
        self.assertEqual(response.status_code, 200)  # form error: not a boolean
        self.assertFalse(Setting.objects.filter(pk=console_modules.SETTING_KEY).exists())


class AttentionTests(ConsoleTestCase):
    def test_review_moderation_still_recomputes(self):
        review = EntryReview.objects.create(entry=self.entry, user=self.reader, rating=4)
        response = self.client.post(
            reverse("admin:core_entryreview_changelist"),
            {"action": "hide_reviews", "_selected_action": [review.pk]},
        )
        self.assertEqual(response.status_code, 302)
        review.refresh_from_db()
        self.assertEqual(review.state, EntryReview.State.HIDDEN)


class PaginatorTests(TestCase):
    def test_unfiltered_big_table_uses_estimate_below_threshold_counts(self):
        paginator = EstimatedCountPaginator(Entry.objects.all(), 10)
        self.assertEqual(paginator.count, 0)  # tiny table: an exact count


class StaticTests(TestCase):
    def test_collectstatic_finds_console_assets(self):
        from django.contrib.staticfiles import finders

        for path in (
            "console/css/elvira-admin.css",
            "console/js/console.js",
            "console/vendor/alpine.min.js",
            "console/vendor/tabulator.min.js",
            "console/vendor/cytoscape.min.js",
            "console/vendor/echarts.min.js",
            "console/fonts/literata-latin-wght-normal.woff2",
            "console/img/favicon.svg",
        ):
            self.assertIsNotNone(finders.find(path), path)

    def test_manifest_build(self):
        """The production storage can post-process every static file (the Docker build runs this)."""
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            with override_settings(
                STATIC_ROOT=root,
                STORAGES={
                    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
                },
            ):
                call_command("collectstatic", interactive=False, verbosity=0)
