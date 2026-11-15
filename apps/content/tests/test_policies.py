"""#174: versioned policy documents, presets, resolution, REST admin, assignment and explain."""

from django.test import TestCase, override_settings

from apps.content import policies
from apps.content.access import decide
from apps.content.tests.fixtures import ALLOWED_RANGES, INSIDE, OUTSIDE, Scenarios, request_for
from apps.core import settings_store
from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, Catalog, User, UserCatalog


def _tier(request, entry, key, processor=None):
    return decide(request, entry, processor=processor, policy=policies.require(key)).tier


@override_settings(EVILFLOWERS_ALLOWED_IP_RANGES=ALLOWED_RANGES)
class PresetTests(TestCase):
    """The tier each preset gives an open, an LCP, an IP-blocked and a borrowed entry (#174 "Done when")."""

    @classmethod
    def setUpTestData(cls):
        Scenarios.build(cls)

    def setUp(self):
        policies.invalidate()
        settings_store.invalidate()
        self.addCleanup(policies.invalidate)
        self.addCleanup(settings_store.invalidate)

    def test_the_five_presets_exist_and_everything_resolves_to_parity(self):
        self.assertEqual(
            {policy.key for policy in policies.all_current()},
            {"parity", "open-access", "licensed-standard", "licensed-strict", "campus-only"},
        )
        for policy in policies.all_current():
            self.assertEqual(policy.version, 1)
            policies.validate(policy.key, policy.document)
        resolution = policies.resolve(self.open)
        self.assertEqual((resolution.policy.ref, resolution.source), ("parity@1", "instance"))

    def test_tiers_per_preset(self):
        outside, inside = request_for(self.reader, OUTSIDE), request_for(self.reader, INSIDE)
        expected = {
            #                  open    lcp        ip (outside)  lcp_loan   lcp_returned
            "parity": ("full", "metadata", "metadata", "metadata", "metadata"),
            "open-access": ("full", "full", "full", "full", "full"),
            "licensed-standard": ("full", "metadata", "outline", "full", "passage"),
            "licensed-strict": ("full", "metadata", "outline", "snippet", "metadata"),
            "campus-only": ("full", "metadata", "metadata", "full", "passage"),
        }
        for key, tiers in expected.items():
            with self.subTest(policy=key):
                actual = (
                    _tier(inside, self.open, key),
                    _tier(inside, self.lcp, key),
                    _tier(outside, self.ip, key),
                    _tier(inside, self.lcp_loan, key),
                    _tier(inside, self.lcp_returned, key),
                )
                self.assertEqual(actual, tiers)

    def test_external_processors_are_capped_by_the_licensed_presets(self):
        inside = request_for(self.reader, INSIDE)
        self.assertEqual(_tier(inside, self.lcp_loan, "licensed-standard", processor="external"), "snippet")
        self.assertEqual(_tier(inside, self.lcp_loan, "licensed-strict", processor="external"), "metadata")
        self.assertEqual(_tier(inside, self.open, "parity", processor="external"), "full")

    def test_an_api_key_processor_and_the_instance_default(self):
        key = ApiKey.objects.create(user=self.reader, name="claude")
        request = request_for(self.reader, INSIDE, api_key=key)
        override = policies.require("licensed-strict")
        self.assertEqual(decide(request, self.lcp_loan, policy=override).processor, "external")
        settings_store.update("content.external_processor_default", "local")
        self.assertEqual(decide(request, self.lcp_loan, policy=override).tier, "snippet")

    def test_a_member_counts_as_inside_under_licensed_standard_but_not_campus_only(self):
        outside = request_for(self.member, OUTSIDE)
        self.assertEqual(_tier(outside, self.ip, "licensed-standard"), "full")
        self.assertEqual(_tier(outside, self.ip, "campus-only"), "metadata")

    def test_a_past_borrower_gets_the_budget_of_the_policy(self):
        decision = decide(
            request_for(self.reader, INSIDE), self.lcp_returned, policy=policies.require("licensed-standard")
        )
        self.assertEqual(decision.basis, "past_loan")
        self.assertEqual(decision.budget, {"bases": ["past_loan"], "percent_of_book": 10, "window_days": 30})

    def test_resolution_goes_entry_then_catalog_then_instance(self):
        Catalog.objects.filter(pk=self.public.pk).update(content_policy="licensed-standard")
        self.open.refresh_from_db()
        self.assertEqual(policies.resolve(self.open).source, "catalog")
        self.open.config = {**self.open.config, "content_policy": "open-access"}
        self.assertEqual(policies.resolve(self.open).policy.key, "open-access")
        self.open.config = {**self.open.config, "content_policy": "deleted-policy"}
        resolution = policies.resolve(self.open)
        self.assertEqual((resolution.policy.key, resolution.skipped), ("licensed-standard", ("entry:deleted-policy",)))


class PolicyStoreTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Another suite's TransactionTestCase may have flushed the migration's rows.
        policies.ensure_presets()

    def setUp(self):
        policies.invalidate()
        self.addCleanup(policies.invalidate)

    def test_a_save_is_a_new_version_and_old_versions_stay_readable(self):
        document = dict(policies.PRESETS["licensed-standard"])
        document["tiers"] = {**document["tiers"], "past_loan": "snippet"}
        policy, changed = policies.save("licensed-standard", document)
        self.assertTrue(changed)
        self.assertEqual(policy.ref, "licensed-standard@2")
        self.assertEqual(policies.current("licensed-standard").tier("past_loan"), "snippet")
        self.assertEqual(policies.version("licensed-standard", 1).tier("past_loan"), "passage")
        self.assertEqual([item["version"] for item in policies.versions("licensed-standard")], [2, 1])
        self.assertFalse(policies.save("licensed-standard", document)[1])

    def test_an_invalid_document_is_refused_with_its_schema_path(self):
        document = {**policies.PRESETS["parity"], "tiers": {**policies.PRESETS["parity"]["tiers"], "loan": "all"}}
        with self.assertRaises(policies.PolicyError) as refused:
            policies.save("parity", document)
        self.assertEqual(refused.exception.path, "tiers/loan")
        self.assertEqual(refused.exception.schema_path, "properties/tiers/properties/loan/enum")
        with self.assertRaises(policies.PolicyError):
            policies.save("other-key", policies.PRESETS["parity"])
        with self.assertRaises(policies.PolicyError):
            policies.save("inherit", {**policies.PRESETS["parity"], "key": "inherit"})

    def test_parity_has_a_builtin_fallback(self):
        from apps.content.models import ContentPolicy

        ContentPolicy.objects.filter(key="parity").delete()
        policies.invalidate()
        self.assertEqual(policies.current("parity").ref, "parity@0")


@override_settings(EVILFLOWERS_ALLOWED_IP_RANGES=ALLOWED_RANGES)
class PolicyApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Scenarios.build(cls)
        cls.admin = User.objects.create(
            username="admin", name="A", surname="D", auth_source=cls.owner.auth_source, is_superuser=True
        )
        cls.manager = User.objects.create(username="manager", name="M", surname="D", auth_source=cls.owner.auth_source)
        UserCatalog.objects.create(user=cls.manager, catalog=cls.public, mode=UserCatalog.Mode.MANAGE)

    def setUp(self):
        policies.invalidate()
        self.addCleanup(policies.invalidate)

    def headers(self, user) -> dict:
        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}", "X-Forwarded-For": INSIDE}

    def test_list_get_schema_and_versions(self):
        listed = self.client.get("/api/v1/content-policies", headers=self.headers(self.reader)).json()["response"]
        self.assertEqual(len(listed), 5)
        schema = self.client.get("/api/v1/content-policies/schema", headers=self.headers(self.reader)).json()
        self.assertIn("tiers", schema["response"]["properties"])
        detail = self.client.get("/api/v1/content-policies/parity", headers=self.headers(self.reader)).json()
        self.assertEqual(detail["response"]["ref"], "parity@1")
        versions = self.client.get("/api/v1/content-policies/parity/versions", headers=self.headers(self.reader))
        self.assertEqual(versions.json()["response"][0]["ref"], "parity@1")
        missing = self.client.get("/api/v1/content-policies/nope", headers=self.headers(self.reader))
        self.assertEqual(missing.status_code, 404)

    def test_only_superusers_create_and_edit(self):
        document = {**policies.PRESETS["licensed-strict"], "key": "faculty", "title": "Faculty"}
        body = {"document": document}
        refused = self.client.post(
            "/api/v1/content-policies", body, content_type="application/json", headers=self.headers(self.manager)
        )
        self.assertEqual(refused.status_code, 403)
        created = self.client.post(
            "/api/v1/content-policies", body, content_type="application/json", headers=self.headers(self.admin)
        )
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["response"]["ref"], "faculty@1")
        document["budget"] = {"bases": ["past_loan"], "percent_of_book": 5, "window_days": 14}
        updated = self.client.put(
            "/api/v1/content-policies/faculty",
            {"document": document},
            content_type="application/json",
            headers=self.headers(self.admin),
        )
        self.assertEqual(updated.json()["response"]["ref"], "faculty@2")

    def test_an_invalid_document_is_refused_with_the_schema_path(self):
        document = {**policies.PRESETS["parity"], "caps": {**policies.PRESETS["parity"]["caps"], "outside_ip": 3}}
        response = self.client.put(
            "/api/v1/content-policies/parity",
            {"document": document},
            content_type="application/json",
            headers=self.headers(self.admin),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["additional_data"]["path"], "caps/outside_ip")

    def test_a_manager_assigns_to_their_catalog_and_entries_only(self):
        assign = self.client.post(
            "/api/v1/content-policies/open-access/assign",
            {"catalog_id": str(self.public.pk), "entry_ids": [str(self.open.pk)]},
            content_type="application/json",
            headers=self.headers(self.manager),
        )
        self.assertEqual(assign.status_code, 200, assign.content)
        self.public.refresh_from_db()
        self.open.refresh_from_db()
        self.assertEqual(self.public.content_policy, "open-access")
        self.assertEqual(self.open.config["content_policy"], "open-access")

        refused = self.client.post(
            "/api/v1/content-policies/open-access/assign",
            {"catalog_id": str(self.public.pk)},
            content_type="application/json",
            headers=self.headers(self.reader),
        )
        self.assertEqual(refused.status_code, 403)

        cleared = self.client.post(
            "/api/v1/content-policies/inherit/assign",
            {"entry_ids": [str(self.open.pk)]},
            content_type="application/json",
            headers=self.headers(self.manager),
        )
        self.assertEqual(cleared.status_code, 200)
        self.open.refresh_from_db()
        self.assertNotIn("content_policy", self.open.config)

    def test_explain_shows_the_basis_policy_caps_and_tier(self):
        Catalog.objects.filter(pk=self.public.pk).update(content_policy="open-access")
        explained = self.client.post(
            "/api/v1/content-policies/explain",
            {"entry_id": str(self.lcp.pk)},
            content_type="application/json",
            headers=self.headers(self.reader),
        ).json()["response"]
        self.assertEqual(explained["policy"], "policy:open-access@1")
        self.assertEqual(explained["policy_source"], "catalog")
        self.assertEqual((explained["basis"], explained["tier"]), ("none", "full"))

        for_other = self.client.post(
            "/api/v1/content-policies/explain",
            {"entry_id": str(self.lcp_loan.pk), "user_id": str(self.reader.pk), "policy": "licensed-standard"},
            content_type="application/json",
            headers=self.headers(self.admin),
        ).json()["response"]
        self.assertEqual(
            (for_other["basis"], for_other["tier"], for_other["user_id"]), ("loan", "full", str(self.reader.pk))
        )

        refused = self.client.post(
            "/api/v1/content-policies/explain",
            {"entry_id": str(self.open.pk), "user_id": str(self.admin.pk)},
            content_type="application/json",
            headers=self.headers(self.reader),
        )
        self.assertEqual(refused.status_code, 403)
        hidden = self.client.post(
            "/api/v1/content-policies/explain",
            {"entry_id": str(self.hidden.pk)},
            content_type="application/json",
            headers=self.headers(self.reader),
        )
        self.assertEqual(hidden.status_code, 404)
