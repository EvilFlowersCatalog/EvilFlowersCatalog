"""
IP-020 / #101: the search surfaces — result highlights and metadata, fallbacks, `order_by=relevance`,
`include=facets`, the facet dimensions, `/entries/suggest`, index maintenance and OPDS facets.
"""

import json

from lxml import etree

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import EmptyResultSet
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Count, Exists, Max, Min, OuterRef, Q
from django.test import RequestFactory, TestCase
from partial_date import PartialDate

from apps.api.filters.entries import fulltext_runs
from apps.api.services.facets import EntryFacetService, count_together
from apps.core.models import Author, Catalog, Category, Entry, EntryAuthor, Feed
from apps.core.tests.test_search_golden import SearchFixtureMixin

ATOM = "{http://www.w3.org/2005/Atom}"
OPDS = "{http://opds-spec.org/2010/catalog}"
THR = "{http://purl.org/syndication/thread/1.0}"
FACET_REL = "http://opds-spec.org/facet"


class SearchApiTests(SearchFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_corpus()
        cls.feed = Feed.objects.create(
            catalog=cls.catalog,
            creator=cls.owner,
            title={"sk": "Informatika", "en": "Computing"},
            url_name="it",
            kind="acquisition",
            source="relation",
            content="",
        )
        cls.feed.entries.add(*[cls.keys[key] for key in ("clrs", "algoritmy", "ml", "strojove")])
        Entry.objects.filter(pk=cls.keys["clrs"]).update(publisher="MIT Press")
        Entry.objects.filter(pk=cls.keys["ml"]).update(publisher="McGraw-Hill")

        # A private catalog the anonymous reader must never see, not even through suggestions.
        private = Catalog.objects.create(creator=cls.owner, title="Private", url_name="private", is_public=False)
        secret = Entry.objects.create(creator=cls.owner, catalog=private, title="Secret Algorithms")
        EntryAuthor.objects.create(
            entry=secret,
            author=Author.objects.create(catalog=private, name="Hidden", surname="Algorithmist"),
            position=0,
        )

    def get(self, path, **params):
        # Localized facet titles follow Accept-Language.
        response = self.client.get(path, params, headers={"Accept-Language": "sk"})
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response

    def entries(self, **params):
        return self.get("/api/v1/entries", catalog_id=str(self.catalog.pk), **params).json()

    def item(self, payload, key):
        return next(item for item in payload["items"] if item["id"] == str(self.keys[key]))

    # --- results ---------------------------------------------------------------------------

    def test_items_carry_score_matched_fields_and_highlights(self):
        payload = self.entries(query="vzdelávania")
        hit = self.item(payload, "vzdelavanie")["search"]
        self.assertGreater(hit["score"], 0)
        self.assertIn("summary", hit["matched"])
        self.assertIn("<mark>vzdelávania</mark>", hit["highlights"]["summary"])
        # the summary's own HTML is stripped and the rest escaped: only <mark> remains as markup
        self.assertNotIn("<p>", hit["highlights"]["summary"])
        self.assertNotIn("<b>", hit["highlights"]["summary"])

    def test_title_highlight_without_diacritics_in_query(self):
        hit = self.item(self.entries(query="vzdelavanie"), "vzdelavanie")["search"]
        self.assertIn("title", hit["matched"])
        self.assertIn("<mark>Vzdelávanie</mark>", hit["highlights"]["title"])

    def test_author_and_category_matches_are_reported(self):
        self.assertIn("author", self.item(self.entries(query="Cormen"), "clrs")["search"]["matched"])
        self.assertIn("category", self.item(self.entries(query="informatika"), "clrs")["search"]["matched"])

    def test_metadata_reports_strict_search(self):
        search = self.entries(query="climate")["metadata"]["search"]
        self.assertEqual(search["query"], "climate")
        # `bm25` where pg_search is installed (IP-021), `fulltext` (ts_rank_cd) elsewhere; no TEI here.
        self.assertIn(search["mode"], ("fulltext", "bm25"))
        self.assertFalse(search["relaxed"])
        self.assertIsNone(search["fallback"])
        self.assertEqual(search["total"], 2)

    def test_no_entry_with_every_word_falls_back_to_any_word(self):
        payload = self.entries(query="climate zebra")
        self.assertTrue(payload["metadata"]["search"]["relaxed"])
        self.assertEqual(payload["metadata"]["search"]["fallback"], "any_word")
        self.assertEqual(payload["items"][0]["id"], str(self.keys["climate"]))

    def test_typo_falls_back_to_similar_titles_and_suggests_a_correction(self):
        payload = self.entries(query="Hirshberger")
        self.assertEqual(payload["metadata"]["search"]["fallback"], "similar")
        self.assertEqual(payload["items"][0]["id"], str(self.keys["philosophie"]))
        self.assertEqual(payload["metadata"]["search"]["did_you_mean"], "hirschberger")

    def test_nothing_similar_returns_nothing(self):
        payload = self.entries(query="qqqxxxzzz")
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["metadata"]["search"]["total"], 0)

    def test_negated_word_is_excluded_in_every_language(self):
        ids = [item["id"] for item in self.entries(query="climate -architecture")["items"]]
        self.assertEqual(ids, [str(self.keys["climate"])])

    def test_list_without_query_has_no_search_block(self):
        payload = self.entries()
        self.assertIsNone(payload["metadata"]["search"])
        self.assertNotIn("search", payload["items"][0])

    def test_order_by_relevance_is_accepted_with_and_without_query(self):
        ranked = self.entries(query="algebra matematika", order_by="relevance")
        self.assertEqual(ranked["metadata"]["search"]["query"], "algebra matematika")
        self.assertEqual(self.entries(order_by="relevance")["metadata"]["total"], len(self.keys))

    def test_explicit_order_overrides_relevance(self):
        ids = [item["id"] for item in self.entries(query="climates", order_by="-popularity")["items"]]
        self.assertEqual(ids, [str(self.keys["climate"]), str(self.keys["sustainable"])])

    def test_unfilled_opensearch_parameters_are_ignored(self):
        payload = self.entries(query="climate", category_id="{ef:category_id?}", language_code="{ef:language_code?}")
        self.assertEqual(payload["metadata"]["total"], 2)
        self.assertEqual(self.entries(query="climate", language_code="*")["metadata"]["total"], 2)

    # --- facets ----------------------------------------------------------------------------

    def test_include_facets_in_list_response(self):
        payload = self.entries(query="algorithms", include="facets")
        self.assertIn("facets", payload)
        authors = {row["surname"] for row in payload["facets"]["authors"]}
        self.assertIn("Cormen", authors)

    def test_list_without_include_has_no_facets(self):
        self.assertIsNone(self.entries()["facets"])

    def test_facet_dimensions(self):
        facets = self.get("/api/v1/entries/facets", catalog_id=str(self.catalog.pk)).json()["response"]
        self.assertEqual(sum(row["count"] for row in facets["languages"]), len(self.keys))
        informatika = next(row for row in facets["categories"] if row["term"] == "004")
        self.assertEqual(informatika["count"], 5)
        self.assertEqual(informatika["label"]["sk"], "Informatika")
        self.assertEqual(facets["feeds"][0]["count"], 4)
        self.assertEqual(len(facets["authors"]), 10)  # top-N by default
        self.assertEqual({row["value"] for row in facets["publishers"]}, {"MIT Press", "McGraw-Hill"})
        self.assertIsInstance(facets["years"], dict)

    def test_facet_subset_with_facet_query(self):
        facets = self.get(
            "/api/v1/entries/facets", catalog_id=str(self.catalog.pk), facet="authors", facet_query="bielik"
        ).json()["response"]
        self.assertEqual([row["surname"] for row in facets["authors"]], ["Bieliková"])
        self.assertIsNone(facets["languages"])

    def test_facets_follow_the_query(self):
        facets = self.get(
            "/api/v1/entries/facets", catalog_id=str(self.catalog.pk), query="architecture", facet="languages"
        ).json()["response"]
        # `architecture` stems to the English title; the category label Architecture matches the Slovak one too
        self.assertEqual(sum(row["count"] for row in facets["languages"]), 2)

    def test_facets_in_one_statement_match_each_query_alone(self):
        """#135: the `UNION ALL` of the dimensions returns what each dimension's own query returns."""
        Entry.objects.filter(pk=self.keys["clrs"]).update(published_at=PartialDate("1990"))
        Entry.objects.filter(pk=self.keys["climate"]).update(published_at=PartialDate("2021-06"))
        catalog = str(self.catalog.pk)
        cases = (
            {"catalog_id": catalog},
            {"catalog_id": catalog, "query": "algorithms", "language_code": "en"},
            {"catalog_id": catalog, "query": "informatika", "feed_id": str(self.feed.pk), "facet_limit": "2"},
            {"catalog_id": catalog, "facet_query": "a", "facet_limit": "3"},
            {"catalog_id": catalog, "query": "qqqxxxzzz"},
        )
        for params in cases:
            with self.subTest(params=params):
                request = RequestFactory().get("/api/v1/entries/facets", params)
                request.user = AnonymousUser()
                service = EntryFacetService(request)
                service._resolve_search()
                branches = service.branches()
                together = count_together(branches)
                normalise = lambda rows: json.loads(json.dumps(list(rows), cls=DjangoJSONEncoder))  # noqa: E731
                for name, qs in branches.items():
                    try:
                        alone = normalise(qs)
                    except EmptyResultSet:
                        alone = []
                    self.assertEqual(together[name], alone, name)

                # `years` and `fulltext` against how they were computed before (aggregate, PartialDate years).
                bounds = service.entries_without("published_at__gte", "published_at__lte").aggregate(
                    min=Min("published_at"), max=Max("published_at")
                )
                counts = service.entries_without("fulltext").aggregate(
                    total=Count("pk"),
                    extracted=Count(
                        "pk", filter=Q(Exists(fulltext_runs().filter(acquisition__entry_id=OuterRef("pk"))))
                    ),
                )
                facets = service.compute()
                self.assertEqual(
                    facets["years"], {key: value.date.year if value else None for key, value in bounds.items()}
                )
                expected = [{"value": True, "count": counts["extracted"]}]
                expected.append({"value": False, "count": counts["total"] - counts["extracted"]})
                self.assertEqual(facets["fulltext"], [row for row in expected if row["count"]])

    def test_years_facet_reads_partial_dates(self):
        Entry.objects.filter(pk=self.keys["clrs"]).update(published_at=PartialDate("1990"))
        Entry.objects.filter(pk=self.keys["ml"]).update(published_at=PartialDate("1997-03-01"))
        facets = self.get("/api/v1/entries/facets", catalog_id=str(self.catalog.pk), facet="years").json()
        self.assertEqual(facets["response"]["years"]["min"], 1990)
        self.assertEqual(facets["response"]["years"]["max"], 1997)

    def test_publisher_and_author_filters(self):
        self.assertEqual(self.entries(publisher="mit press")["metadata"]["total"], 1)
        author = Author.objects.get(surname="Cormen")
        self.assertEqual(self.entries(author_id=str(author.pk))["metadata"]["total"], 1)

    # --- suggest ---------------------------------------------------------------------------

    def test_suggest_returns_entries_authors_categories_and_feeds(self):
        payload = self.get("/api/v1/entries/suggest", query="informat").json()["response"]
        self.assertIn(str(self.keys["woerterbuch"]), [entry["id"] for entry in payload["entries"]])
        self.assertEqual(payload["categories"][0]["term"], "004")
        self.assertEqual(payload["categories"][0]["count"], 5)
        self.assertEqual(payload["feeds"][0]["count"], 4)

    def test_suggest_matches_the_last_word_as_a_prefix(self):
        payload = self.get("/api/v1/entries/suggest", query="strojové uč").json()["response"]
        self.assertEqual(payload["entries"][0]["id"], str(self.keys["strojove"]))
        self.assertIn("<mark>", payload["entries"][0]["highlight"])
        self.assertEqual(payload["entries"][0]["authors"], ["Mária Bieliková"])

    def test_suggest_finds_authors(self):
        payload = self.get("/api/v1/entries/suggest", query="bielik").json()["response"]
        self.assertEqual([author["surname"] for author in payload["authors"]], ["Bieliková"])
        self.assertEqual(payload["authors"][0]["count"], 1)

    def test_suggest_never_shows_private_catalogs(self):
        payload = self.get("/api/v1/entries/suggest", query="algorithm").json()["response"]
        titles = [entry["title"] for entry in payload["entries"]]
        self.assertNotIn("Secret Algorithms", titles)
        self.assertNotIn("Algorithmist", [author["surname"] for author in payload["authors"]])

    def test_suggest_did_you_mean(self):
        payload = self.get("/api/v1/entries/suggest", query="Hirshbergr").json()["response"]
        self.assertEqual(payload["entries"], [])
        self.assertEqual(payload["did_you_mean"], "hirschberger")

    # --- index maintenance ----------------------------------------------------------------

    def test_index_follows_author_rename(self):
        author = Author.objects.get(surname="Cormen")
        author.surname = "Leiserson"
        author.save()
        self.assertEqual(self.entries(query="Leiserson")["items"][0]["id"], str(self.keys["clrs"]))
        self.assertEqual(self.entries(query="Cormen")["items"], [])

    def test_index_follows_category_changes(self):
        entry = Entry.objects.get(pk=self.keys["physik"])
        self.assertNotIn(str(entry.pk), [item["id"] for item in self.entries(query="meteorology")["items"]])
        entry.categories.add(self.categories["551"])
        self.assertIn(str(entry.pk), [item["id"] for item in self.entries(query="meteorology")["items"]])

        category = self.categories["53"]
        category.label = {"sk": "Fyzika", "en": "Physics", "de": "Quantenwelt"}
        category.save()
        self.assertEqual(self.entries(query="Quantenwelt")["items"][0]["id"], str(entry.pk))

        category.entries.clear()
        self.assertEqual(self.entries(query="Quantenwelt")["items"], [])

    def test_index_follows_entry_edits(self):
        entry = Entry.objects.get(pk=self.keys["chemia"])
        entry.title = "Anorganická chémia"
        entry.save()
        self.assertEqual(self.entries(query="anorganicka")["items"][0]["id"], str(entry.pk))

    def test_search_vector_is_not_loaded_with_entries(self):
        entry = Entry.objects.get(pk=self.keys["chemia"])
        self.assertIn("search_vector", entry.get_deferred_fields())

    # --- OPDS ------------------------------------------------------------------------------

    def test_opds12_search_feed_has_facet_links(self):
        response = self.get("/opds/v1.2/open/search", query="informatika")
        feed = etree.fromstring(response.content)
        facets = [link for link in feed.findall(f"{ATOM}link") if link.get("rel") == FACET_REL]
        self.assertTrue(facets)
        groups = {link.get(f"{OPDS}facetGroup") for link in facets}
        self.assertIn("Author", groups)
        self.assertTrue(all(int(link.get(f"{THR}count")) > 0 for link in facets))

    def test_opds12_active_facet(self):
        category = self.categories["004"]
        response = self.get("/opds/v1.2/open/search", query="informatika", category_id=str(category.pk))
        feed = etree.fromstring(response.content)
        active = [link for link in feed.findall(f"{ATOM}link") if link.get(f"{OPDS}activeFacet") == "true"]
        self.assertEqual([link.get("title") for link in active], ["Informatika"])

    def test_opensearch_descriptor_declares_filters(self):
        content = self.get("/opds/v1.2/open/search.xml").content
        self.assertIn(b"query={searchTerms}", content)
        self.assertIn(b"language_code={ef:language_code?}", content)
        self.assertIn(b"category_id={ef:category_id?}", content)
        self.assertIn(b'xmlns:ef="', content)

    def test_opds2_search_feed_has_facets(self):
        payload = self.get("/opds/v2/open/search", query="informatika").json()
        groups = {facet["metadata"]["title"]: facet["links"] for facet in payload["facets"]}
        self.assertIn("Category", groups)
        informatika = next(link for link in groups["Category"] if link["title"] == "Informatika")
        self.assertEqual(informatika["properties"]["numberOfItems"], 5)

    def test_opds2_publications_feed_filters_and_marks_active_facet(self):
        language_code = "de"
        payload = self.get("/opds/v2/open/publications", language_code=language_code).json()
        self.assertEqual(payload["metadata"]["numberOfItems"], 4)
        languages = next(facet for facet in payload["facets"] if facet["metadata"]["title"] == "Language")
        active = [link for link in languages["links"] if link.get("rel") == "self"]
        self.assertEqual(len(active), 1)
