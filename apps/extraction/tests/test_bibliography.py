"""#178: reference lists parsed from extracted text, linked to catalog entries, cited in the text."""

from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase, TestCase

from apps.assistant.tests import fixtures as books
from apps.content import policies
from apps.core.models import Entry
from apps.extraction import bibliography, locators
from apps.extraction.models import Bibitem, Paragraph, Reference

#: Real reference styles from the dev library, joined the way the extraction leaves them.
VANCOUVER = (
    "translate direct cardiac reprogramming. 1. Gurdon JB. The developmental capacity of nuclei taken from "
    "intestinal epithelium cells of feeding tadpoles. J Embryol Exp Morphol. 1962;10:622–40. 2. Takahashi K, "
    "Yamanaka S. Induction of pluripotent stem cells from mouse embryonic and adult fibroblast cultures by defined "
    "factors. Cell. 2006;126:663–76. 3. Sancho-Martinez I, Baek SH, Izpisua Belmonte JC. Lineage conversion "
    "methodologies meet the reprogramming toolbox. Nat Cell Biol. 2012;14:892–9."
)
LNCS = (
    "1. Adee, S.: The hunt for the kill switch. IEEE Spectr. 45(5), 34–39 (2008) 2. BBC: "
    "http://www.bbc.com/news/business-34324772 3. Greenwald, G.: No Place to Hide: Edward Snowden, the NSA, and the "
    "US Surveillance State. Macmillan, Basingstoke (2014) 4. New York Times: Secret back door in some U.S. phones "
    "sent data to China, analysts say. http://www.nytimes.com/2016/11/16/us/politics/china.html"
)
APA = (
    "Asbridge, M. (2004). Public place restrictions on smoking in Canada: Assessing the role of the state. Social "
    "Science & Medicine, 58, 13–24. https://doi.org/10.1016/S0277-9536(03)00154-0 Baba, A., Cook, D. M., & Bero, "
    "L. A. (2005). Legislating “Sound Science”: The role of the tobacco industry. American Journal of Public Health, "
    "95, S20–S27. https://doi.org/10.2105/AJPH.2004.050963 FAO. (2016). The state of world fisheries and "
    "aquaculture 2016. Rome: FAO."
)
SPRINGER = (
    "Altshuler D, Daly MJ, Lander ES (2008) Genetic mapping in human disease. Science 322:881–888 Amable B (2003) "
    "The diversity of modern capitalism. Oxford University Press, Oxford Barro RJ (2000) Inequality and growth in a "
    "panel of countries. J Econ Growth 5(1):5–32 doi:10. 1016/j.jue.2006.04.005"
)
CHICAGO = (
    "ABIM Foundation, ACP–ASIM Foundation, and European Federation of Internal Medicine. 2002. Medical "
    "professionalism in the new millennium: A physician charter. Annals of Internal Medicine 136: 243–246. "
    "Bartlett, R., and S. Collins. 2011. Aristotle’s Nicomachean ethics . Chicago: University of Chicago Press. "
    "Beauchamp, D.E. 1976. Public health as social justice. Inquiry 13(1): 3–14."
)


class SplitTests(SimpleTestCase):
    def titles(self, text):
        return [item.title for item in bibliography.split(text)]

    def test_vancouver(self):
        items = bibliography.split(VANCOUVER)
        self.assertEqual([item.number for item in items], ["1", "2", "3"])
        self.assertEqual(items[0].authors, ["Gurdon JB"])
        self.assertEqual(items[0].year, 1962)
        self.assertTrue(items[1].title.startswith("Induction of pluripotent stem cells"))

    def test_lncs_keeps_abbreviations_inside_titles(self):
        items = bibliography.split(LNCS)
        self.assertEqual(items[0].title, "The hunt for the kill switch")
        self.assertEqual(items[0].year, 2008)
        self.assertEqual(items[2].title, "No Place to Hide: Edward Snowden, the NSA, and the US Surveillance State")
        self.assertTrue(items[3].title.startswith("Secret back door in some U.S. phones"))

    def test_apa_with_dois_and_an_institution(self):
        items = bibliography.split(APA)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].doi, "10.1016/s0277-9536(03)00154-0")
        self.assertEqual(items[1].authors[:2], ["Baba, A.", "Cook, D. M."])
        self.assertEqual(items[2].authors, ["FAO"])
        self.assertTrue(items[2].title.startswith("The state of world fisheries"))

    def test_springer_items_without_a_full_stop_and_a_broken_doi(self):
        items = bibliography.split(SPRINGER)
        self.assertEqual([item.year for item in items], [2008, 2003, 2000])
        self.assertEqual(items[2].authors, ["Barro RJ"])
        self.assertEqual(items[2].doi, "10.1016/j.jue.2006.04.005")

    def test_chicago_bare_years(self):
        items = bibliography.split(CHICAGO)
        self.assertEqual([item.year for item in items], [2002, 2011, 1976])
        self.assertEqual(items[1].title, "Aristotle’s Nicomachean ethics")
        self.assertEqual(items[1].authors[0], "Bartlett, R.")

    def test_csl(self):
        item = bibliography.split(APA)[1]
        csl = item.csl()
        self.assertEqual(csl["author"][0], {"family": "Baba", "given": "A."})
        self.assertEqual(csl["issued"], {"date-parts": [[2005]]})
        self.assertEqual(csl["DOI"], "10.2105/ajph.2004.050963")

    def test_endnotes_before_an_author_year_list_do_not_swallow_it(self):
        # Run 0d73c89b on dev, untrimmed: three chapter endnotes, then the APA reference list.
        text = (
            "1. For the sake of simplicity I refer to the “Ministry of Health” and “Health Minister” throughout the "
            "book. However, this ministry had several names in the past. 2. See: “The end of tobacco? The tobacco "
            "endgame.” Special issue. Tobacco Control, May 2013, vol. 22, Suppl. 1. 3. Accessible through "
            "https://industrydocuments.library.ucsf.edu/tobacco/ collections/dutch-tobacco-industry/ " + APA
        )
        items = bibliography.split(text)
        self.assertEqual([item.number for item in items[:3]], ["1", "2", "3"])
        self.assertTrue(items[2].raw.startswith("Accessible through"))
        self.assertNotIn("Asbridge", items[2].raw)
        references = items[3:]
        self.assertEqual([item.year for item in references], [2004, 2005, 2016])
        self.assertTrue(all(item.number is None for item in references))
        self.assertTrue(references[0].title.startswith("Public place restrictions on smoking"))

    def test_two_numbered_lists_in_one_block_restart_at_one(self):
        first = "1. Adee S. A first work. J A. 2001;1:1–2. 2. Bee T. A second work. J B. 2002;2:3–4. 3. Cee U. A third work. J C. 2003;3:5–6."
        items = bibliography.split(first + " " + VANCOUVER)
        self.assertEqual([item.number for item in items], ["1", "2", "3", "1", "2", "3"])
        self.assertEqual(items[2].title, "A third work")
        self.assertTrue(items[3].title.startswith("The developmental capacity"))

    def test_prose_under_a_reference_heading_is_not_an_item(self):
        prose = (
            "As shown in the previous chapter (2011), the results were later confirmed by the working group. "
            "This chapter therefore builds on those findings. "
        )
        items = bibliography.split(prose + APA)
        self.assertEqual([item.year for item in items], [2004, 2005, 2016])

    def test_reference_headings(self):
        for heading in ("References", "REFERENCES", "Bibliografia", "Použitá literatúra", "Literaturverzeichnis"):
            self.assertTrue(bibliography.is_reference_section(heading), heading)
        for heading in ("Literature Review", "2 Literature Review", "Introduction", None):
            self.assertFalse(bibliography.is_reference_section(heading), heading)


class BuildTests(TestCase):
    """A processed book whose last chapter is a numbered reference list split across paragraphs."""

    @classmethod
    def setUpTestData(cls):
        policies.ensure_presets()
        cls.reader = books.user()
        cls.catalog = books.catalog(cls.reader)
        cls.cited = Entry.objects.create(
            creator=cls.reader, catalog=cls.catalog, title="Cited book", identifiers={"doi": "doi:10.1234/CITED.1"}
        )
        paragraphs = list(books.FLUIDS) + [
            (5, "2 Bernoulli", "The effect was measured before [2] and explained in [1, 3]; see also Kates (2011)."),
            (
                6,
                "References",
                "1. Gurdon JB. The developmental capacity of nuclei. J Embryol. 1962;10:622–40. 2. Takahashi K,",
            ),
            (
                6,
                "References",
                "Yamanaka S. Induction of pluripotent stem cells. Cell. 2006;126:663–76. doi:10.1234/cited.1",
            ),
            (7, "References", "3. Sancho-Martinez I. Lineage conversion methodologies. Nat Cell Biol. 2012;14:892–9."),
        ]
        cls.entry, cls.entry_run, cls.paragraphs, *_rest = books.publication(
            cls.reader, cls.catalog, paragraphs=paragraphs
        )

    def test_build_parses_matches_and_links_markers(self):
        counts = bibliography.build(self.entry_run)
        self.assertEqual(
            {key: counts[key] for key in ("blocks", "items", "titled", "matched")},
            {"blocks": 1, "items": 3, "titled": 3, "matched": 1},
        )
        items = list(Bibitem.objects.filter(run=self.entry_run).order_by("seq"))
        self.assertEqual([item.number for item in items], ["1", "2", "3"])
        self.assertEqual((items[1].entry_id, items[1].page, items[1].doi), (self.cited.pk, 6, "10.1234/cited.1"))
        self.assertEqual(items[2].page, 7)
        markers = Reference.objects.filter(run=self.entry_run, kind=Reference.Kind.BIBITEM)
        self.assertEqual(sorted(marker.bibitem.number for marker in markers), ["1", "2", "3"])

        again = bibliography.build(self.entry_run)
        self.assertEqual(again["items"], 3)
        self.assertEqual(Bibitem.objects.filter(run=self.entry_run).count(), 3)

    def test_other_references_survive_and_bibitems_get_locators(self):
        from apps.extraction import references

        bibliography.build(self.entry_run)
        references.build(self.entry_run)
        self.assertEqual(Reference.objects.filter(run=self.entry_run, kind=Reference.Kind.BIBITEM).count(), 3)

        locators.assign(self.entry_run)
        item = Bibitem.objects.get(run=self.entry_run, number="2")
        resolution = locators.resolve(self.entry.pk, item.locator_key)
        self.assertEqual((resolution.resolution, resolution.bibitem.pk), ("exact", item.pk))

    def test_reading_surfaces(self):
        from apps.content import reading
        from apps.graph.projection import _cited_entries
        from apps.mcp import uris

        bibliography.build(self.entry_run)
        locators.assign(self.entry_run)
        request = RequestFactory().get("/")
        request.user = AnonymousUser()

        outline = reading.outline(request, self.entry.pk)
        self.assertEqual(outline["references"]["count"], 3)
        self.assertEqual(outline["references"]["items"][1]["cites_entry_id"], str(self.cited.pk))

        item = Bibitem.objects.get(run=self.entry_run, number="2")
        citation = reading.citation_for(request, uris.locator_uri(self.entry.pk, item.locator_key))
        self.assertEqual(citation["kind"], "bibitem")
        self.assertEqual(citation["locator"]["label"], "[2]")
        self.assertEqual(citation["reference"]["in_library"]["entry_id"], str(self.cited.pk))

        found = bibliography.citations_of(request, self.cited.pk)
        self.assertEqual([row["entry_id"] for row in found["cited_by"]], [str(self.entry.pk)])
        self.assertEqual(bibliography.citations_of(request, self.entry.pk)["cites"][0]["entry_id"], str(self.cited.pk))
        self.assertEqual(
            _cited_entries(self.entry_run), [{"id": str(self.cited.pk), "title": "Cited book", "items": 1}]
        )

    def test_crossref_unavailable_is_asked_again(self):
        from apps.content import rights

        bibliography.build(self.entry_run)
        with mock.patch("apps.content.rights.crossref_work", side_effect=rights.CrossrefUnavailable("timeout")):
            self.assertTrue(bibliography.enrich_run(self.entry_run.pk)["unavailable"])
        self.assertFalse(Bibitem.objects.filter(run=self.entry_run, crossref_at__isnull=False).exists())

    def test_the_counts_are_kept_on_the_run(self):
        bibliography.build(self.entry_run)
        self.entry_run.refresh_from_db()
        self.assertEqual(self.entry_run.provenance["bibliography"]["titled"], 3)

    def test_a_cited_entry_in_a_hidden_catalog_is_not_revealed(self):
        from apps.content import reading

        hidden = books.catalog(self.reader, public=False, name="vault")
        Entry.objects.filter(pk=self.cited.pk).update(catalog=hidden)
        bibliography.build(self.entry_run)
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        outline = reading.outline(request, self.entry.pk)
        self.assertFalse(any("cites_entry_id" in item for item in outline["references"]["items"]))
        self.assertEqual(bibliography.citations_of(request, self.entry.pk)["cites"], [])

    def test_crossref_enrichment(self):
        bibliography.build(self.entry_run)
        message = {
            "title": ["Induction of pluripotent stem cells"],
            "container-title": ["Cell"],
            "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/", "content-version": "vor"}],
        }
        with mock.patch("apps.content.rights.crossref_work", return_value=message) as fetch:
            counts = bibliography.enrich_run(self.entry_run.pk)
        fetch.assert_called_once_with("10.1234/cited.1", strict=True)
        self.assertEqual(counts, {"enriched": 1, "rights": 1})
        item = Bibitem.objects.get(run=self.entry_run, number="2")
        self.assertEqual((item.csl["container-title"], item.csl["source"]), ("Cell", "crossref"))
        self.cited.refresh_from_db()
        self.assertEqual(self.cited.rights["source"], "crossref")
        with mock.patch("apps.content.rights.crossref_work") as fetch:
            bibliography.enrich_run(self.entry_run.pk)
        fetch.assert_not_called()

    def test_post_processing_runs_the_bibliography_first(self):
        from apps.extraction import tasks

        with (
            mock.patch("apps.graph.projection.project_run", return_value={}),
            mock.patch("apps.extraction.regions.index_run", return_value={}),
            mock.patch("apps.extraction.similarity.compute_run", return_value={}),
            mock.patch.object(tasks.enrich_bibliography, "delay") as enrich,
        ):
            report = tasks.post_process_run(str(self.entry_run.pk))
        self.assertEqual(report["bibliography"]["items"], 3)
        self.assertEqual(report["version"], tasks.POST_PROCESS_VERSION)
        enrich.assert_called_once_with(str(self.entry_run.pk))
        self.assertTrue(Bibitem.objects.filter(run=self.entry_run).exclude(locator_key=None).exists())
        self.assertEqual(Paragraph.objects.filter(run=self.entry_run).count(), len(self.paragraphs))
