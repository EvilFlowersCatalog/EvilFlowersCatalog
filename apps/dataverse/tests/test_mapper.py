"""Unit tests for `apps.dataverse.services.mapper`."""

from datetime import datetime

import pytest
from partial_date import PartialDate

from apps.core.models import Acquisition
from apps.dataverse.services.mapper import (
    extract_metadata,
    first_text,
    flatten_text,
    map_content_type_to_mime,
    parse_partial_date,
)


class TestMapContentTypeToMime:
    def test_pdf(self):
        assert map_content_type_to_mime("application/pdf") == Acquisition.AcquisitionMIME.PDF

    def test_epub(self):
        assert map_content_type_to_mime("application/epub+zip") == Acquisition.AcquisitionMIME.EPUB

    def test_unknown_defaults_to_pdf(self):
        assert map_content_type_to_mime("application/x-weird") == Acquisition.AcquisitionMIME.PDF
        assert map_content_type_to_mime(None) == Acquisition.AcquisitionMIME.PDF


class TestFlattenText:
    def test_none(self):
        assert flatten_text(None) == []

    def test_str(self):
        assert flatten_text("  hello  ") == ["hello"]
        assert flatten_text("") == []

    def test_list_of_strings(self):
        assert flatten_text(["a", "", "b"]) == ["a", "b"]

    def test_first_last_name_dict(self):
        result = flatten_text({"firstName": "Jane", "lastName": "Doe"})
        assert result == ["Jane Doe"]

    def test_value_dict(self):
        result = flatten_text({"value": "x"})
        assert result == ["x"]


class TestFirstText:
    def test_returns_first_non_empty(self):
        assert first_text(None, "", "hi", "world") == "hi"

    def test_all_empty(self):
        assert first_text(None, "", []) is None


class TestParsePartialDate:
    def test_full(self):
        result = parse_partial_date("2026-05-25")
        assert isinstance(result, PartialDate)
        assert (result.year, result.month, result.day) == (2026, 5, 25)

    def test_year_month(self):
        result = parse_partial_date("2026-05")
        assert (result.year, result.month) == (2026, 5)

    def test_year_only(self):
        result = parse_partial_date("2026")
        assert result.year == 2026

    def test_invalid(self):
        assert parse_partial_date("hello") is None
        assert parse_partial_date(None) is None
        assert parse_partial_date("") is None


@pytest.mark.django_db
class TestExtractMetadata:
    def test_minimal(self):
        result = extract_metadata({}, [], global_id="doi:10.5072/STU/ABC")
        assert result["title"] == "Dataverse Dataset doi:10.5072/STU/ABC"
        assert result["authors"] == []
        assert result["summary"] is None

    def test_title_override(self):
        result = extract_metadata({}, [], title_override="Override Title", global_id="X")
        assert result["title"] == "Override Title"

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
        assert result["authors"] == [
            {"name": "Ada", "surname": "Lovelace"},
            {"name": "Alan", "surname": "Turing"},
        ]

    def test_doi_normalization(self):
        result = extract_metadata({"persistentId": "10.5072/STU/ABC"}, [], global_id="X")
        assert result["doi"] == "doi:10.5072/STU/ABC"

    def test_summary_from_description(self):
        result = extract_metadata({"description": "  A dataset  "}, [], global_id="X")
        assert result["summary"] == "A dataset"
