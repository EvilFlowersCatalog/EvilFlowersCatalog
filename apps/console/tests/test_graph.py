"""IP-035: the knowledge-graph explorer reads the relational tables behind the `library` graph."""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.assistant.models import SectionSummary
from apps.assistant.tests import fixtures
from apps.core import settings_store
from apps.core.models import Category
from apps.decider.models import Band, SectionRelation
from apps.extraction.models import Locator
from apps.graph.projection import concept_key, section_key

NO_AGE = mock.patch("apps.console.views.graph._age_available", return_value=False)


@override_settings(EVILFLOWERS_EXTRACTION_ENABLED=True)
class GraphExplorerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = fixtures.user("root")
        cls.admin.is_superuser = True
        cls.admin.save()
        cls.catalog = fixtures.catalog(cls.admin)
        cls.fluids, cls.fluids_run, *_ = fixtures.publication(cls.admin, cls.catalog, title="Fluid Mechanics")
        cls.optics, cls.optics_run, *_ = fixtures.publication(cls.admin, cls.catalog, title="Optics")
        cls.category = Category.objects.create(creator=cls.admin, catalog=cls.catalog, term="532")
        cls.fluids.categories.add(cls.category)
        cls.intro = section_key(cls.fluids.pk, "1 Introduction")
        cls.bernoulli = section_key(cls.optics.pk, "2 Bernoulli")
        cls.hidden = section_key(cls.optics.pk, "1 Introduction")
        for entry, key, heading in (
            (cls.fluids, cls.intro, "1 Introduction"),
            (cls.optics, cls.bernoulli, "2 Bernoulli"),
            (cls.optics, cls.hidden, "1 Introduction"),
        ):
            Locator.objects.create(key=key, entry=entry, kind=Locator.Kind.SECTION, source_page=1, quote_exact=heading)
        SectionRelation.objects.create(
            a_key=cls.intro,
            b_key=cls.bernoulli,
            relation=SectionRelation.Relation.PREREQUISITE,
            p=0.91,
            decider_model="m",
        )
        SectionRelation.objects.create(
            a_key=cls.intro,
            b_key=cls.hidden,
            relation=SectionRelation.Relation.NONE,
            p=0.97,
            band=Band.HIDE,
            decider_model="m",
        )
        SectionSummary.objects.create(
            run=cls.fluids_run,
            entry=cls.fluids,
            section="1 Introduction",
            first_page=1,
            last_page=2,
            summary="About fluids.",
            concepts=[{"term": "Shear stress", "role": "defines"}],
            digest="x",
        )

    def setUp(self):
        cache.clear()
        settings_store.invalidate()
        self.client.force_login(self.admin)

    def expand(self, node, **params):
        return self.client.get(reverse("admin:console_graph_expand"), {"node": node, **params})

    def test_page_renders(self):
        response = self.client.get(reverse("admin:console_graph"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "console/vendor/cytoscape.min.js")
        self.assertContains(response, "graph-edge-types")

    def test_search_finds_entries_categories_and_concepts(self):
        with NO_AGE:
            data = self.client.get(reverse("admin:console_graph_search"), {"q": "flu"}).json()
            self.assertEqual(data["source"], "relational")
            self.assertIn(f"entry:{self.fluids.pk}", {item["id"] for item in data["results"]})
            data = self.client.get(reverse("admin:console_graph_search"), {"q": "shear"}).json()
        self.assertIn(f"concept:{concept_key('Shear stress')}", {item["id"] for item in data["results"]})

    def test_entry_expansion_without_age(self):
        with NO_AGE:
            response = self.expand(f"entry:{self.fluids.pk}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["source"], "relational")
        types = {edge["type"] for edge in data["edges"]}
        self.assertIn("FILED_UNDER", types)
        self.assertIn("HAS_SECTION", types)
        ids = {node["id"] for node in data["nodes"]}
        self.assertIn(f"category:{self.category.pk}", ids)
        self.assertIn(f"section:{self.intro}", ids)
        for edge in data["edges"]:
            self.assertIn(edge["source"], ids)
            self.assertIn(edge["target"], ids)

    def test_section_expansion_types_and_hides(self):
        with NO_AGE:
            data = self.expand(f"section:{self.intro}").json()
        edges = {(edge["type"], edge["source"], edge["target"]) for edge in data["edges"]}
        self.assertIn(("PREREQUISITE_OF", f"section:{self.intro}", f"section:{self.bernoulli}"), edges)
        self.assertNotIn(f"section:{self.hidden}", {node["id"] for node in data["nodes"]})
        # Scoped by the entry's UDC main class (category "532" → "5"), as the projection keys it (#153).
        concept = f"concept:{concept_key('Shear stress', '5')}"
        self.assertIn(("MENTIONS", f"section:{self.intro}", concept), edges)
        self.assertIn(("HAS_SECTION", f"entry:{self.fluids.pk}", f"section:{self.intro}"), edges)

        with NO_AGE:
            data = self.expand(f"section:{self.intro}", hidden="1").json()
        hidden = [edge for edge in data["edges"] if edge["type"] == "UNRELATED"]
        self.assertEqual(len(hidden), 1)
        self.assertEqual(hidden[0]["band"], "hide")

    def test_concept_expansion_from_summaries(self):
        for key in (concept_key("Shear stress"), concept_key("Shear stress", "5")):
            with NO_AGE:
                data = self.expand(f"concept:{key}").json()
            self.assertEqual(data["source"], "relational", key)
            self.assertIn(f"section:{self.intro}", {node["id"] for node in data["nodes"]})
        with NO_AGE:
            self.assertEqual(self.expand(f"concept:{concept_key('Shear stress', '3')}").status_code, 404)

    def test_category_expansion_is_capped(self):
        with NO_AGE:
            data = self.expand(f"category:{self.category.pk}", limit=1).json()
        self.assertEqual(
            [node["id"] for node in data["nodes"] if node["kind"] == "entry"], [f"entry:{self.fluids.pk}"]
        )

    def test_bad_nodes(self):
        self.assertEqual(self.expand("entry:not-a-uuid").status_code, 404)
        self.assertEqual(self.expand("planet:x").status_code, 400)
        self.assertEqual(self.expand("nothing").status_code, 400)

    def test_overview(self):
        data = self.client.get(reverse("admin:console_graph_overview")).json()
        self.assertEqual(data["entries"], 2)
        self.assertEqual(data["typed_relations"], 2)

    def test_module_off_is_404(self):
        from apps.console import modules

        settings_store.update(modules.SETTING_KEY, {"knowledge": False})
        self.addCleanup(settings_store.invalidate)
        self.assertEqual(self.client.get(reverse("admin:console_graph")).status_code, 404)
        self.assertEqual(self.expand(f"entry:{self.fluids.pk}").status_code, 404)

    def test_reader_cannot_enter(self):
        reader = fixtures.user("reader")
        self.client.force_login(reader)
        self.assertEqual(self.expand(f"entry:{self.fluids.pk}").status_code, 302)
