"""IP-035: the vector explorer — PCA map, cosine neighbours, coverage, passage search."""

from unittest import mock

import numpy as np
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.assistant.tests import fixtures
from apps.console.views.vectors import pca_2d
from apps.core import settings_store
from apps.core.models import Entry
from apps.core.models.entry import ENTRY_EMBEDDING_DIMENSIONS


def unit(*leading) -> list:
    vector = np.zeros(ENTRY_EMBEDDING_DIMENSIONS)
    vector[: len(leading)] = leading
    return (vector / np.linalg.norm(vector)).tolist()


class PcaTests(SimpleTestCase):
    def test_projects_onto_the_main_axis(self):
        points, explained = pca_2d([[0, 0, 0], [1, 0, 0], [2, 0, 0], [3, 0.01, 0]])
        self.assertEqual(points.shape, (4, 2))
        self.assertGreater(explained[0], 0.99)
        xs = points[:, 0]
        self.assertTrue(all(np.diff(xs) > 0) or all(np.diff(xs) < 0))

    def test_degenerate_inputs(self):
        self.assertEqual(pca_2d([])[0].shape, (0, 2))
        points, explained = pca_2d([[1, 2, 3]])
        self.assertEqual(points.tolist(), [[0.0, 0.0]])
        self.assertEqual(explained, [0.0, 0.0])


class VectorExplorerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = fixtures.user("root")
        cls.admin.is_superuser = True
        cls.admin.save()
        cls.catalog = fixtures.catalog(cls.admin)
        cls.base = Entry.objects.create(creator=cls.admin, catalog=cls.catalog, title="Base")
        cls.near = Entry.objects.create(creator=cls.admin, catalog=cls.catalog, title="Near")
        cls.far = Entry.objects.create(creator=cls.admin, catalog=cls.catalog, title="Far")
        cls.other_model = Entry.objects.create(creator=cls.admin, catalog=cls.catalog, title="Other model")
        cls.bare = Entry.objects.create(creator=cls.admin, catalog=cls.catalog, title="No vector")
        for entry, vector, model in (
            (cls.base, unit(1, 0, 0), "e5@1"),
            (cls.near, unit(1, 0.1, 0), "e5@1"),
            (cls.far, unit(0, 0, 1), "e5@1"),
            (cls.other_model, unit(1, 0, 0), "bge@1"),
        ):
            Entry.objects.filter(pk=entry.pk).update(embedding=vector, embedding_model=model)

    def setUp(self):
        cache.clear()
        settings_store.invalidate()
        self.client.force_login(self.admin)

    def test_page_renders(self):
        response = self.client.get(reverse("admin:console_vectors"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "console/vendor/echarts.min.js")

    def test_map_projects_one_model_to_2d(self):
        data = self.client.get(reverse("admin:console_vectors_map"), {"source": "entry", "colour": "catalog"}).json()
        self.assertEqual(data["model"], "e5@1")
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["excluded_other_models"], 1)
        self.assertEqual(len(data["points"]), 3)
        for point in data["points"]:
            self.assertIsInstance(point["x"], float)
            self.assertIsInstance(point["y"], float)
        self.assertEqual(data["groups"][0]["label"], self.catalog.title)
        self.assertEqual(len(data["explained_variance"]), 2)

    def test_map_validates_parameters(self):
        self.assertEqual(self.client.get(reverse("admin:console_vectors_map"), {"source": "x"}).status_code, 400)
        self.assertEqual(self.client.get(reverse("admin:console_vectors_map"), {"colour": "x"}).status_code, 400)

    def test_run_map_without_runs_is_empty(self):
        data = self.client.get(reverse("admin:console_vectors_map"), {"source": "run"}).json()
        self.assertEqual(data["points"], [])

    def test_neighbours_ordered_by_distance_same_model(self):
        data = self.client.get(
            reverse("admin:console_vectors_neighbours"), {"entry": str(self.base.pk), "k": 5}
        ).json()
        titles = [item["title"] for item in data["items"]]
        self.assertEqual(titles, ["Near", "Far"])
        distances = [item["distance"] for item in data["items"]]
        self.assertEqual(distances, sorted(distances))
        self.assertAlmostEqual(data["items"][0]["similarity"], 1 - data["items"][0]["distance"], places=4)

    def test_neighbours_of_an_entry_without_vector(self):
        response = self.client.get(reverse("admin:console_vectors_neighbours"), {"entry": str(self.bare.pk)})
        self.assertEqual(response.status_code, 404)
        response = self.client.get(reverse("admin:console_vectors_neighbours"), {"entry": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_entry_picker(self):
        data = self.client.get(reverse("admin:console_vectors_entries"), {"q": "near"}).json()
        self.assertEqual([item["title"] for item in data["results"]], ["Near"])

    def test_coverage(self):
        data = self.client.get(reverse("admin:console_vectors_coverage")).json()
        entries = next(item for item in data["items"] if item["model"] == "core.Entry")
        self.assertEqual(entries["rows"], 5)
        self.assertEqual(entries["with_vector"], 4)
        self.assertFalse(entries["estimated"])
        self.assertEqual(entries["dimensions"], ENTRY_EMBEDDING_DIMENSIONS)
        self.assertEqual({item["model"] for item in entries["models"]}, {"e5@1", "bge@1"})
        self.assertEqual(
            {item["model"] for item in data["items"]},
            {"core.Entry", "extraction.ExtractionRun", "extraction.Region", "extraction.Paragraph"},
        )

    @override_settings(EVILFLOWERS_EMBEDDING_URL="")
    def test_passage_search_degrades_without_tei(self):
        fixtures.publication(self.admin, self.catalog, title="Fluid Mechanics")
        data = self.client.get(reverse("admin:console_vectors_passages"), {"q": "Bernoulli pressure"}).json()
        self.assertTrue(data["degraded"])
        self.assertTrue(data["items"])
        self.assertEqual(data["items"][0]["title"], "Fluid Mechanics")

    def test_passage_search_reports_failures(self):
        with mock.patch("apps.extraction.discovery.search_passages", side_effect=RuntimeError("boom")):
            response = self.client.get(reverse("admin:console_vectors_passages"), {"q": "anything"})
        self.assertEqual(response.status_code, 502)

    def test_module_off_is_404(self):
        from apps.console import modules

        settings_store.update(modules.SETTING_KEY, {"library": False})
        self.addCleanup(settings_store.invalidate)
        self.assertEqual(self.client.get(reverse("admin:console_vectors")).status_code, 404)
        self.assertEqual(self.client.get(reverse("admin:console_vectors_map")).status_code, 404)
