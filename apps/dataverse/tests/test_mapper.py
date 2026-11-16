"""Unit tests for `apps.dataverse.services.mapper`."""

from django.test import SimpleTestCase, TestCase
from partial_date import PartialDate

from apps.core.models import Acquisition
from apps.dataverse.services.mapper import (
    extract_metadata,
    first_text,
    flatten_text,
    map_content_type_to_mime,
    parse_partial_date,
)


class MapContentTypeToMimeTests(SimpleTestCase):
    def test_pdf(self):
        self.assertEqual(map_content_type_to_mime("application/pdf"), Acquisition.AcquisitionMIME.PDF)

    def test_epub(self):
        self.assertEqual(map_content_type_to_mime("application/epub+zip"), Acquisition.AcquisitionMIME.EPUB)

    def test_unknown_defaults_to_pdf(self):
        self.assertEqual(map_content_type_to_mime("application/x-weird"), Acquisition.AcquisitionMIME.PDF)
        self.assertEqual(map_content_type_to_mime(None), Acquisition.AcquisitionMIME.PDF)


class FlattenTextTests(SimpleTestCase):
    def test_none(self):
        self.assertEqual(flatten_text(None), [])

    def test_str(self):
        self.assertEqual(flatten_text("  hello  "), ["hello"])
        self.assertEqual(flatten_text(""), [])

    def test_list_of_strings(self):
        self.assertEqual(flatten_text(["a", "", "b"]), ["a", "b"])

    def test_first_last_name_dict(self):
        self.assertEqual(flatten_text({"firstName": "Jane", "lastName": "Doe"}), ["Jane Doe"])

    def test_value_dict(self):
        self.assertEqual(flatten_text({"value": "x"}), ["x"])


class FirstTextTests(SimpleTestCase):
    def test_returns_first_non_empty(self):
        self.assertEqual(first_text(None, "", "hi", "world"), "hi")

    def test_all_empty(self):
        self.assertIsNone(first_text(None, "", []))


class ParsePartialDateTests(SimpleTestCase):
    def test_full(self):
        # `PartialDate` exposes the date and its precision (no `year`/`month`/`day` attributes).
        result = parse_partial_date("2026-05-25")
        self.assertIsInstance(result, PartialDate)
        self.assertEqual((result.date.year, result.date.month, result.date.day), (2026, 5, 25))
        self.assertTrue(result.precisionDay())

    def test_year_month(self):
        result = parse_partial_date("2026-05")
        self.assertEqual((result.date.year, result.date.month), (2026, 5))
        self.assertTrue(result.precisionMonth())

    def test_year_only(self):
        result = parse_partial_date("2026")
        self.assertEqual(result.date.year, 2026)
        self.assertTrue(result.precisionYear())

    def test_invalid(self):
        self.assertIsNone(parse_partial_date("hello"))
        self.assertIsNone(parse_partial_date(None))
        self.assertIsNone(parse_partial_date(""))


class ExtractMetadataTests(TestCase):
    def test_minimal(self):
        result = extract_metadata({}, [], global_id="doi:10.5072/STU/ABC")
        self.assertEqual(result["title"], "Dataverse Dataset doi:10.5072/STU/ABC")
        self.assertEqual(result["authors"], [])
        self.assertIsNone(result["summary"])

    def test_title_override(self):
        result = extract_metadata({}, [], title_override="Override Title", global_id="X")
        self.assertEqual(result["title"], "Override Title")

    def test_authors_from_citation_block(self):
        dataset = {
            "metadataBlocks": {
                "citation": {
                    "fields": [
                        {
                            "typeName": "author",
                            "value": [
                                {"firstName": "Ada", "lastName": "Lovelace"},
                                {"firstName": "Alan", "lastName": "Turing"},
                            ],
                        }
                    ]
                }
            }
        }
        result = extract_metadata(dataset, [], global_id="X")
        self.assertEqual(
            result["authors"],
            [
                {"name": "Ada", "surname": "Lovelace"},
                {"name": "Alan", "surname": "Turing"},
            ],
        )

    def test_doi_normalization(self):
        result = extract_metadata({"persistentId": "10.5072/STU/ABC"}, [], global_id="X")
        self.assertEqual(result["doi"], "doi:10.5072/STU/ABC")

    def test_summary_from_description(self):
        result = extract_metadata({"description": "  A dataset  "}, [], global_id="X")
        self.assertEqual(result["summary"], "A dataset")
