"""#186 follow-up: references print the bare ISBN/DOI, never the stored URN; no-author BibTeX keys use the title."""

from django.test import SimpleTestCase

from apps.content import citation
from apps.harvest.records import normalize_doi, normalize_isbn
from apps.opds.services.identifiers import doi_uri

URI = "evilflowers://entry/d828e95c-4572-4c40-936c-8af2f80ef93c/locator/p100-par01"
URL = "https://dev.elvira.stuba.sk/cite/d828e95c-4572-4c40-936c-8af2f80ef93c/p100-par01"
TITLE = "Methods for Measuring Greenhouse Gas Balances and Evaluating Mitigation Options in Smallholder Agriculture"


def item(**work):
    return {"uri": URI, "url": URL, "work": {"title": TITLE, **work}, "locator": {"page": 100}}


class NormaliseIdentifierTests(SimpleTestCase):
    def test_urn_isbn_normalises(self):
        # The split on ":" left "urn" before, so OAI and /resolve/isbn dropped every URN-stored ISBN.
        self.assertEqual(normalize_isbn("urn:isbn:9783319297927"), "9783319297927")
        self.assertEqual(normalize_isbn("URN:ISBN:3-319-29792-9"), "9783319297927")
        self.assertEqual(normalize_isbn("ISBN 978-3-319-29792-7"), "9783319297927")
        self.assertEqual(normalize_isbn("ISBN: 978-3-319-29792-7"), "9783319297927")
        # A MARC qualifier after the ISBN is still ignored.
        self.assertEqual(normalize_isbn("3-319-29792-9 (brož.)"), "9783319297927")

    def test_urn_doi_normalises(self):
        self.assertEqual(normalize_doi("urn:doi:10.1007/978-3-319-29794-1"), "10.1007/978-3-319-29794-1")
        self.assertEqual(doi_uri("urn:doi:10.1007/x"), "https://doi.org/10.1007/x")


class WorkIdentifierTests(SimpleTestCase):
    def test_isbn_is_hyphenated_without_urn(self):
        self.assertEqual(citation._isbn("urn:isbn:9783319297927"), "978-3-319-29792-7")

    def test_invalid_isbn_is_kept_without_prefix(self):
        self.assertEqual(citation._isbn("urn:isbn:9783319297920"), "9783319297920")

    def test_doi_drops_urn_and_resolver(self):
        self.assertEqual(citation._doi("urn:doi:10.1007/x"), "10.1007/x")
        self.assertEqual(citation._doi("https://dx.doi.org/10.1007/x"), "10.1007/x")


class FormattedReferenceTests(SimpleTestCase):
    def test_iso690_and_bibtex_print_the_bare_isbn(self):
        reference = item(ISBN=citation._isbn("urn:isbn:9783319297927"))
        iso = citation.iso690(reference)
        bib = citation.bibtex(reference)

        self.assertIn("ISBN 978-3-319-29792-7.", iso)
        self.assertNotIn("urn:", iso)
        self.assertIn("isbn = {978-3-319-29792-7}", bib)

    def test_bibtex_key_without_author_comes_from_the_title(self):
        bib = citation.bibtex(item())
        self.assertTrue(bib.startswith("@book{Methods"), bib)

    def test_bibtex_key_with_author_is_unchanged(self):
        bib = citation.bibtex(
            item(author=[{"family": "Rosenstock", "given": "Todd S."}], issued={"date-parts": [[2016]]})
        )
        self.assertTrue(bib.startswith("@book{Rosenstock2016"), bib)
