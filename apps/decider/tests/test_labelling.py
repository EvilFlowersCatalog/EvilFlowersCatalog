"""#201: read endpoints for labelling the decision model — section text by key, raw section pairs, region keys."""

from django.test import TestCase
from django.urls import reverse

from apps.assistant.tests import fixtures
from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, UserCatalog
from apps.decider.models import Band, SectionRelation
from apps.extraction import locators
from apps.extraction.models import SectionSimilarity
from apps.graph.projection import section_key


def headers(user) -> dict:
    key = ApiKey.objects.create(user=user, name="test")
    return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}


class LabellingEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = fixtures.user("admin")
        cls.admin.is_superuser = True
        cls.admin.save()
        cls.manager = fixtures.user("manager")
        cls.reader = fixtures.user("reader")
        cls.catalog = fixtures.catalog(cls.admin)
        cls.other_catalog = fixtures.catalog(cls.admin, name="other")
        UserCatalog.objects.create(user=cls.manager, catalog=cls.catalog, mode=UserCatalog.Mode.MANAGE)
        UserCatalog.objects.create(user=cls.reader, catalog=cls.catalog, mode=UserCatalog.Mode.READ)
        cls.fluids, cls.fluids_run, *_ = fixtures.publication(cls.admin, cls.catalog)
        cls.pipes, cls.pipes_run, *_ = fixtures.publication(cls.admin, cls.catalog, title="Pipe Flow")
        cls.foreign, cls.foreign_run, *_ = fixtures.publication(cls.admin, cls.other_catalog, title="Hydraulics")
        for run in (cls.fluids_run, cls.pipes_run, cls.foreign_run):
            locators.assign(run)
        SectionSimilarity.objects.create(
            source=cls.fluids_run,
            source_section="2 Bernoulli",
            target=cls.pipes_run,
            target_section="2 Bernoulli",
            score=0.9,
        )
        cls.bernoulli = (section_key(cls.fluids.pk, "2 Bernoulli"), section_key(cls.pipes.pk, "2 Bernoulli"))
        cls.intro = (section_key(cls.fluids.pk, "1 Introduction"), section_key(cls.pipes.pk, "1 Introduction"))
        cls.cross = (section_key(cls.fluids.pk, "2 Bernoulli"), section_key(cls.foreign.pk, "2 Bernoulli"))
        SectionRelation.objects.create(
            a_key=cls.bernoulli[0], b_key=cls.bernoulli[1], relation="pre", p=0.8, band=Band.KEEP, decider_model="m@1"
        )
        # Hidden from readers, so `/sections/{key}/related` would never show it.
        SectionRelation.objects.create(
            a_key=cls.intro[1], b_key=cls.intro[0], relation="none", p=0.95, band=Band.HIDE, decider_model="m@1"
        )
        SectionRelation.objects.create(
            a_key=cls.cross[0], b_key=cls.cross[1], relation="loose", p=0.5, band=Band.KEEP, decider_model="m@1"
        )

    # -- section text --------------------------------------------------------------------------------------

    def test_section_text_by_key(self):
        response = self.client.get(
            reverse("api:section-text", kwargs={"key": self.bernoulli[0]}), headers=headers(self.manager)
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()["response"]
        self.assertEqual(body["title"], "2 Bernoulli")
        self.assertEqual(body["entry_id"], str(self.fluids.pk))
        self.assertEqual((body["first_page"], body["last_page"]), (3, 5))
        self.assertEqual((body["paragraphs"], body["paragraphs_shown"], body["truncated"]), (3, 3, False))
        self.assertTrue(body["text"].startswith("Bernoulli's principle relates pressure and speed."))
        self.assertEqual(len(body["text"].split("\n")), 3)

    def test_section_text_is_cut_at_chars(self):
        response = self.client.get(
            reverse("api:section-text", kwargs={"key": self.bernoulli[0]}) + "?chars=100",
            headers=headers(self.admin),
        )
        body = response.json()["response"]
        self.assertEqual((body["paragraphs_shown"], body["truncated"]), (1, True))
        self.assertLessEqual(len(body["text"]), 100)

    def test_section_text_needs_manage(self):
        response = self.client.get(
            reverse("api:section-text", kwargs={"key": self.bernoulli[0]}), headers=headers(self.reader)
        )
        self.assertEqual(response.status_code, 403)

    def test_unknown_section_is_404(self):
        response = self.client.get(reverse("api:section-text", kwargs={"key": "0" * 20}), headers=headers(self.admin))
        self.assertEqual(response.status_code, 404)

    # -- section pairs -------------------------------------------------------------------------------------

    def pairs(self, user, query=""):
        response = self.client.get(reverse("api:decider-section-pairs") + query, headers=headers(user))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["items"]

    def test_pairs_include_hidden_ones_in_the_sweeps_orientation(self):
        items = {(item["a_key"], item["b_key"]): item for item in self.pairs(self.admin)}
        self.assertEqual(len(items), 3)
        hidden = items[(self.intro[1], self.intro[0])]
        self.assertEqual((hidden["relation"], hidden["band"]), ("none", "hide"))
        self.assertEqual(hidden["a_entry_id"], str(self.pipes.pk))
        self.assertIsNone(hidden["score"])
        typed = items[self.bernoulli]
        self.assertEqual((typed["relation"], typed["p"], typed["score"]), ("pre", 0.8, 0.9))
        self.assertEqual(typed["a_title"], "2 Bernoulli")

    def test_filters(self):
        self.assertEqual([item["relation"] for item in self.pairs(self.admin, "?relation=none")], ["none"])
        self.assertEqual([item["band"] for item in self.pairs(self.admin, "?band=hide")], ["hide"])
        self.assertEqual(len(self.pairs(self.admin, f"?entry_id={self.foreign.pk}")), 1)

    def test_a_manager_sees_only_pairs_inside_their_catalogs(self):
        pairs = {(item["a_key"], item["b_key"]) for item in self.pairs(self.manager)}
        self.assertEqual(pairs, {self.bernoulli, (self.intro[1], self.intro[0])})

    def test_a_reader_may_not_list_pairs(self):
        response = self.client.get(reverse("api:decider-section-pairs"), headers=headers(self.reader))
        self.assertEqual(response.status_code, 403)

    def test_anonymous_callers_are_refused(self):
        for url in (
            reverse("api:decider-section-pairs"),
            reverse("api:section-text", kwargs={"key": self.bernoulli[0]}),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 401)

    def test_bad_filter_is_400(self):
        response = self.client.get(reverse("api:decider-section-pairs") + "?relation=x", headers=headers(self.admin))
        self.assertEqual(response.status_code, 400)

    # -- region locator keys -------------------------------------------------------------------------------

    def test_admin_regions_carry_the_locator_key(self):
        response = self.client.get(
            reverse("api:entry-extraction-regions", kwargs={"entry_id": self.fluids.pk}), headers=headers(self.admin)
        )
        self.assertEqual(response.status_code, 200, response.content)
        items = response.json()["response"]["items"]
        self.assertTrue(items)
        self.assertTrue(all(item["locator_key"] for item in items))
        venturi = next(item for item in items if item["page"] == 4)
        self.assertEqual(
            venturi["locator_key"],
            self.fluids_run.regions.get(page=4).locator_key,
        )
