"""#160: similar publications as `rel="related"` links in OPDS 1.2 and OPDS 2, within what the reader may see.

Four processed books in the public catalog form two pairs (fluids, optics). "Secret", in a private catalog, is the
closest of all to Fluid Mechanics: it must never be a related link, and must not change the visible pairs.
"""

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import TestCase
from lxml import etree

from apps.assistant.tests import fixtures
from apps.core.models import Entry
from apps.graph.tests.test_api import unit
from apps.opds2.schema.rwpm import Publication

ATOM = "{http://www.w3.org/2005/Atom}"


class RelatedLinksTests(TestCase):
    def setUp(self):
        cache.clear()
        self.reader = fixtures.user()
        self.open = fixtures.catalog(self.reader)
        self.hidden = fixtures.catalog(self.reader, public=False, name="hidden")
        books = {}
        for title, catalog, vector in (
            ("Fluid Mechanics", self.open, unit(1, 2)),
            ("Hydraulics", self.open, unit(1, 3)),
            ("Optics", self.open, unit(5, 6)),
            ("Lasers", self.open, unit(5, 7)),
            ("Secret", self.hidden, unit(1, 2, 4)),
        ):
            entry, run, *_ = fixtures.publication(self.reader, catalog, title=title)
            run.embedding = vector
            run.save(update_fields=["embedding"])
            # A complete entry carries the file's checksum.
            run.acquisition.content.save(f"{title}.pdf", ContentFile(b"%PDF-1.4"), save=True)
            books[title] = (entry, run)
        self.fluids, self.fluids_run = books["Fluid Mechanics"]
        self.hydraulics, self.hydraulics_run = books["Hydraulics"]
        self.optics, _ = books["Optics"]
        self.secret, _ = books["Secret"]
        # A book without an extraction run takes no part.
        self.unprocessed = Entry.objects.create(creator=self.reader, catalog=self.open, title="Unprocessed")

    def _related_atom(self, element) -> list:
        return [
            (link.get("href"), link.get("title"))
            for link in element.iter(f"{ATOM}link")
            if link.get("rel") == "related"
        ]

    def _entry_element(self, feed, entry):
        return next(item for item in feed.iter(f"{ATOM}entry") if item.findtext(f"{ATOM}id") == f"urn:uuid:{entry.pk}")

    def test_opds1_feed_and_entry_carry_related_links(self):
        response = self.client.get("/opds/v1.2/open/complete")
        self.assertEqual(response.status_code, 200)
        feed = etree.fromstring(response.content)
        self.assertEqual(
            self._related_atom(self._entry_element(feed, self.fluids)),
            [(f"/opds/v1.2/open/entries/{self.hydraulics.pk}", "Hydraulics")],
        )
        self.assertEqual(self._related_atom(self._entry_element(feed, self.unprocessed)), [])

        entry = etree.fromstring(self.client.get(f"/opds/v1.2/open/entries/{self.optics.pk}").content)
        self.assertEqual([title for _href, title in self._related_atom(entry)], ["Lasers"])

    def test_a_private_publication_is_never_related(self):
        content = self.client.get("/opds/v1.2/open/complete").content
        self.assertNotIn(str(self.secret.pk).encode(), content)
        # Its owner sees it in its own catalog, and the public pairs stay as they are.
        self.client.force_login(self.reader)
        self.reader.catalogs.add(self.hidden, through_defaults={"mode": "manage"})
        feed = etree.fromstring(self.client.get("/opds/v1.2/open/complete").content)
        self.assertEqual(
            [title for _href, title in self._related_atom(self._entry_element(feed, self.fluids))], ["Hydraulics"]
        )
        self.assertNotIn(str(self.secret.pk).encode(), etree.tostring(feed))

    def test_the_etag_changes_with_the_related_links(self):
        etag = self.client.get("/opds/v1.2/open/complete").headers["ETag"]
        self.assertEqual(self.client.get("/opds/v1.2/open/complete", HTTP_IF_NONE_MATCH=etag).status_code, 304)
        # Re-processing (here: the run of Hydraulics retired) changes the neighbours, not the entries.
        self.hydraulics_run.is_active = False
        self.hydraulics_run.save(update_fields=["is_active"])
        response = self.client.get("/opds/v1.2/open/complete", HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._related_atom(self._entry_element(etree.fromstring(response.content), self.fluids)), [])

    def test_opds2_publication_and_feed_carry_related_links(self):
        detail = self.client.get(f"/opds/v2/open/publications/{self.fluids.pk}").json()
        Publication.model_validate(detail)
        related = [link for link in detail["links"] if link.get("rel") == "related"]
        self.assertEqual(
            [(link["href"], link["title"], link["type"]) for link in related],
            [
                (
                    f"http://testserver/opds/v2/open/publications/{self.hydraulics.pk}",
                    "Hydraulics",
                    "application/opds-publication+json",
                )
            ],
        )
        feed = self.client.get("/opds/v2/open/publications").json()
        titles = {
            publication["metadata"]["title"]: [
                link["title"] for link in publication["links"] if link.get("rel") == "related"
            ]
            for publication in feed["publications"]
        }
        self.assertEqual(titles["Optics"], ["Lasers"])
        self.assertEqual(titles["Unprocessed"], [])
        self.assertNotIn(str(self.secret.pk), str(feed))
