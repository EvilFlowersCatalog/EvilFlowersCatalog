"""#202: authors, publisher and year from the ISBN into empty fields only; isbnlib mocked throughout."""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from partial_date import PartialDate

from apps.api.services.entry import EntryService
from apps.core.models import Author, AuthSource, Catalog, Entry, EntryAuthor, User
from apps.core.services import isbn_metadata

ISBN = "9783319297927"
RECORD = {
    "ISBN-13": ISBN,
    "Title": "Statistics for Social Science",
    "Authors": ["Jane Doe", "Novák, Ján"],
    "Publisher": "Springer International Publishing",
    "Year": "2016",
    "Language": "en",
}

# The isbnlib providers (`openl`, `goob`) call `isbnlib.meta`; the HTTP and Z39.50 drivers are tested in
# `test_isbn_providers`.
META = "isbnlib.meta"


class _Form:
    """What `EntryService.populate` reads of an `EntryForm`."""

    def __init__(self, title: str, identifiers: dict, config: dict, **cleaned):
        self.title, self.identifiers, self.config = title, identifiers, config
        self.cleaned_data = cleaned

    def populate(self, entry):
        entry.title = self.title
        entry.identifiers = self.identifiers
        entry.config = {**(entry.config or {}), **self.config}

    def apply_withdrawn(self, entry):
        pass


@override_settings(EVILFLOWERS_ISBN_METADATA_SERVICES=["openl", "goob"], EVILFLOWERS_ISBN_METADATA_DELAY=0)
class IsbnMetadataTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User.objects.create(username="owner", name="O", surname="W", auth_source=source)
        cls.catalog = Catalog.objects.create(creator=cls.user, title="STU", url_name="stu")

    def _entry(self, **kwargs) -> Entry:
        kwargs.setdefault("identifiers", {"isbn": f"urn:isbn:{ISBN}"})
        return Entry.objects.create(
            creator=self.user, catalog=self.catalog, title=kwargs.pop("title", "Book"), **kwargs
        )

    def _authors(self, entry) -> list:
        return [
            (link.author.surname, link.author.name)
            for link in EntryAuthor.objects.filter(entry=entry).select_related("author").order_by("position")
        ]

    def test_urn_isbn_is_normalised_before_the_lookup(self):
        with mock.patch(META, return_value=RECORD) as meta:
            metadata = isbn_metadata.lookup(f"urn:isbn:{ISBN}")
        meta.assert_called_once_with(ISBN, service="openl")
        self.assertEqual(metadata.year, "2016")
        self.assertEqual([(a.surname, a.name) for a in metadata.authors], [("Doe", "Jane"), ("Novák", "Ján")])

    def test_invalid_isbn_asks_no_service(self):
        with mock.patch(META) as meta:
            self.assertIsNone(isbn_metadata.lookup("urn:isbn:123"))
        meta.assert_not_called()

    def test_failing_service_falls_through_to_the_next(self):
        with mock.patch(META, side_effect=[ConnectionError("down"), RECORD]) as meta:
            metadata = isbn_metadata.lookup(ISBN)
        self.assertEqual(meta.call_count, 2)
        self.assertEqual(metadata.service, "goob")

    def test_every_service_failing_returns_none(self):
        with mock.patch(META, side_effect=Exception("quota")):
            self.assertIsNone(isbn_metadata.lookup(ISBN))

    def test_enrich_fills_empty_fields(self):
        entry = self._entry()
        with mock.patch(META, return_value=RECORD):
            found = isbn_metadata.enrich(entry)
        entry.refresh_from_db()
        self.assertEqual(set(found), {"authors", "publisher", "published_at"})
        self.assertEqual(entry.publisher, "Springer International Publishing")
        self.assertEqual(str(entry.published_at), str(PartialDate("2016")))
        self.assertIn("@book", entry.citation)
        self.assertEqual(self._authors(entry), [("Doe", "Jane"), ("Novák", "Ján")])

    def test_enrich_keeps_librarian_values(self):
        entry = self._entry(publisher="STU Press", published_at=PartialDate("2015"))
        author = Author.objects.create(catalog=self.catalog, name="Peter", surname="Librarian")
        EntryAuthor.objects.create(entry=entry, author=author, position=0)
        with mock.patch(META, return_value=RECORD):
            self.assertEqual(isbn_metadata.enrich(entry), {})
        entry.refresh_from_db()
        self.assertEqual(entry.publisher, "STU Press")
        self.assertEqual(str(entry.published_at), str(PartialDate("2015")))
        self.assertEqual(self._authors(entry), [("Librarian", "Peter")])

    def test_dry_run_writes_nothing(self):
        entry = self._entry()
        out = StringIO()
        with mock.patch(META, return_value=RECORD):
            call_command("enrich_from_isbn", "--dry-run", "--catalog", "stu", stdout=out)
        entry.refresh_from_db()
        self.assertIsNone(entry.publisher)
        self.assertIsNone(entry.published_at)
        self.assertIsNone(entry.citation)
        self.assertEqual(self._authors(entry), [])
        self.assertIn("would fill", out.getvalue())
        self.assertIn("Dry run: 1 entries filled", out.getvalue())

    def test_command_skips_complete_entries_and_fills_the_rest(self):
        complete = self._entry(title="Complete", publisher="P", published_at=PartialDate("2001"))
        author = Author.objects.create(catalog=self.catalog, name="A", surname="B")
        EntryAuthor.objects.create(entry=complete, author=author, position=0)
        lacking = self._entry(title="Lacking", identifiers={"isbn": "978-3-319-29792-7"})
        self._entry(title="No ISBN", identifiers={})
        out = StringIO()
        with mock.patch(META, return_value=RECORD) as meta:
            call_command("enrich_from_isbn", stdout=out)
        self.assertEqual(meta.call_count, 1)
        lacking.refresh_from_db()
        self.assertEqual(lacking.publisher, "Springer International Publishing")

    def test_import_fills_from_isbn_when_metadata_fetch_is_on(self):
        entry = Entry(creator=self.user, catalog=self.catalog)
        form = _Form("Imported", {"isbn": f"urn:isbn:{ISBN}"}, {"evilflowers_metadata_fetch": True})
        with mock.patch(META, return_value=RECORD):
            EntryService(self.catalog, self.user).populate(entry, form)
        entry.refresh_from_db()
        self.assertEqual(entry.publisher, "Springer International Publishing")
        self.assertEqual(self._authors(entry), [("Doe", "Jane"), ("Novák", "Ján")])

    def test_import_keeps_the_form_authors_and_publisher(self):
        entry = Entry(creator=self.user, catalog=self.catalog, publisher="Librarian Press")
        form = _Form(
            "Imported",
            {"isbn": ISBN},
            {"evilflowers_metadata_fetch": True},
            authors=[{"name": "Eva", "surname": "Form"}],
        )
        with mock.patch(META, return_value=RECORD):
            EntryService(self.catalog, self.user).populate(entry, form)
        entry.refresh_from_db()
        self.assertEqual(entry.publisher, "Librarian Press")
        self.assertEqual(self._authors(entry), [("Form", "Eva")])
        self.assertEqual(str(entry.published_at), str(PartialDate("2016")))

    def test_import_survives_a_provider_error(self):
        entry = Entry(creator=self.user, catalog=self.catalog)
        form = _Form("Imported", {"isbn": ISBN}, {"evilflowers_metadata_fetch": True})
        with mock.patch(META, side_effect=OSError("network")):
            EntryService(self.catalog, self.user).populate(entry, form)
        entry.refresh_from_db()
        self.assertIsNone(entry.publisher)
        self.assertIsNone(entry.citation)

    def test_import_without_metadata_fetch_asks_nothing(self):
        entry = Entry(creator=self.user, catalog=self.catalog)
        form = _Form("Imported", {"isbn": ISBN}, {})
        with mock.patch(META) as meta:
            EntryService(self.catalog, self.user).populate(entry, form)
        meta.assert_not_called()
