"""
Regression test: publication title/author must reach the encryption worker.

When a borrowed LCP-for-PDF publication (`.lcpdf`) is opened in Thorium, the
reader sources its display title/author from the RWPM `manifest.json` that
lcpencrypt generates inside the encrypted package. A raw PDF carries no
embedded metadata, so unless EFC forwards the Entry's title/author to the
encryption worker, the manifest falls back to the filename (title) and empty
authors — Thorium then shows "no title and no authors available".

This asserts that the payload EFC hands to the lcpencrypt worker in
`ContentEncryptionService._queue_encryption_task` carries the Entry's title
and first author name. The upstream HTTP/Celery hop is mocked via the event
broker so no real worker is contacted.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from partial_date import PartialDate

from apps.readium.services.content_encryption_service import ContentEncryptionService


@override_settings(
    EVILFLOWERS_READIUM_BASE_URL="https://efc.example.org",
    EVILFLOWERS_READIUM_LCPSV_URL="http://user:pass@lcp.example.org",
)
class EncryptionPayloadMetadataTests(SimpleTestCase):
    def _make_encrypted_content(self, *, title, author_name):
        entry = MagicMock()
        entry.title = title
        entry.first_author_name = author_name

        acquisition = MagicMock()
        acquisition.entry = entry
        acquisition.content.name = "catalog/source.pdf"
        acquisition.upload_base_path.return_value = "catalog/base"

        encrypted_content = MagicMock()
        encrypted_content.acquisition = acquisition
        encrypted_content.lcp_content_id = "content-123"
        return encrypted_content

    def _captured_kwargs(self, encrypted_content):
        with patch("apps.readium.services.content_encryption_service.get_event_broker") as mock_get_broker:
            broker = MagicMock()
            mock_get_broker.return_value = broker

            ContentEncryptionService._queue_encryption_task(encrypted_content)

            broker.execute.assert_called_once()
            _event, payload = broker.execute.call_args.args
            return payload["kwargs"]

    def test_payload_includes_title_and_author(self):
        ec = self._make_encrypted_content(title="The Great Gatsby", author_name="F. Scott Fitzgerald")

        kwargs = self._captured_kwargs(ec)

        self.assertEqual(
            kwargs.get("title"),
            "The Great Gatsby",
            "Encryption payload must carry the Entry title so lcpencrypt can "
            "populate the package manifest (Thorium reads title from it).",
        )
        self.assertEqual(
            kwargs.get("author"),
            "F. Scott Fitzgerald",
            "Encryption payload must carry the first author name so lcpencrypt " "can populate the package manifest.",
        )

    def test_missing_author_sends_empty_string_not_crash(self):
        # first_author_name returns "" when the entry has no authors; the
        # payload must still be well-formed (empty author, real title).
        ec = self._make_encrypted_content(title="Anonymous Work", author_name="")

        kwargs = self._captured_kwargs(ec)

        self.assertEqual(kwargs.get("title"), "Anonymous Work")
        self.assertEqual(kwargs.get("author"), "")


def _entry(**fields):
    """A stand-in Entry with explicit metadata (no DB)."""
    entry = MagicMock()
    entry.title = fields.get("title", "Title")
    entry.first_author_name = fields.get("first_author_name", "")
    entry.publisher = fields.get("publisher")
    entry.summary = fields.get("summary")
    entry.identifiers = fields.get("identifiers")
    entry.published_at = fields.get("published_at")

    language = fields.get("language")
    entry.language = SimpleNamespace(alpha2=language) if language else None

    entry_authors = [
        SimpleNamespace(author=SimpleNamespace(full_name=name), position=i)
        for i, name in enumerate(fields.get("authors", []))
    ]
    entry.entry_authors.select_related.return_value.order_by.return_value = entry_authors

    entry.categories.all.return_value = [
        SimpleNamespace(term=term, label=label) for term, label in fields.get("categories", [])
    ]

    image = fields.get("image")
    entry.image = SimpleNamespace(name=image) if image else None
    return entry


@override_settings(EVILFLOWERS_DEFAULT_LANGUAGE="en")
class PublicationMetadataTests(SimpleTestCase):
    """Issue #96: full RWPM metadata for the LCP-for-PDF manifest."""

    def test_full_entry(self):
        entry = _entry(
            title="Algoritmy",
            first_author_name="Ján Novák",
            authors=["Ján Novák", "Eva Malá"],
            publisher="STU",
            summary="Úvod do algoritmov.",
            language="sk",
            published_at=PartialDate(date(2023, 5, 17), PartialDate.DAY),
            categories=[("004", {"sk": "Informatika", "en": "Computer science"}), ("51", {})],
            identifiers={"isbn": "9788022749999", "doi": "10.1000/xyz"},
            image="catalogs/stu/abc/cover.jpg",
        )

        metadata = ContentEncryptionService.publication_metadata(entry)

        self.assertEqual(
            metadata,
            {
                "title": "Algoritmy",
                "author": "Ján Novák",
                "authors": ["Ján Novák", "Eva Malá"],
                "publisher": "STU",
                "description": "Úvod do algoritmov.",
                "language": "sk",
                "published": "2023-05-17",
                "subjects": ["Informatika", "51"],
                "identifier": "urn:isbn:9788022749999",
                "cover": "catalogs/stu/abc/cover.jpg",
            },
        )

    def test_partial_dates_are_not_sent(self):
        for precision in (PartialDate.YEAR, PartialDate.MONTH):
            with self.subTest(precision=precision):
                entry = _entry(published_at=PartialDate(date(2023, 1, 1), precision))
                self.assertNotIn("published", ContentEncryptionService.publication_metadata(entry))

    def test_doi_identifier_when_no_isbn(self):
        entry = _entry(identifiers={"doi": "10.1000/xyz"})
        metadata = ContentEncryptionService.publication_metadata(entry)
        self.assertEqual(metadata["identifier"], "https://doi.org/10.1000/xyz")

    def test_empty_entry_sends_only_title_and_author(self):
        metadata = ContentEncryptionService.publication_metadata(_entry(title="Bare"))
        self.assertEqual(metadata, {"title": "Bare", "author": ""})
