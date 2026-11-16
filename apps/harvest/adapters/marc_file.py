"""MARC files uploaded through `POST harvest/sources/{id}/upload`, for partners without OAI-PMH.

ISO 2709 (`.mrc`) goes through `pymarc.MARCReader`; MARCXML through the hardened parser. A file is a delivery,
not the whole catalogue: records missing from it are never treated as deleted (`exhaustive = False`).
"""

from __future__ import annotations

import io
from typing import Iterator, Optional

from pymarc import MARCReader

from apps.files.storage import get_storage
from apps.harvest.adapters import marc
from apps.harvest.adapters.base import HarvestError, parse_xml
from apps.harvest.adapters.registry import Connector, registry
from apps.harvest.records import SourceRecord


def is_xml(content: bytes) -> bool:
    return content.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")


def read_marc(content: bytes, *, subject_scheme: str) -> Iterator[SourceRecord]:
    """Every record of an ISO 2709 or MARCXML file; an unreadable record comes back with `extra["error"]`."""
    if is_xml(content):
        root = parse_xml(content)
        for index, element in enumerate(marc.records_in(root), start=1):
            yield _map(marc.record_from_element(element), index, subject_scheme)
        return
    reader = MARCReader(io.BytesIO(content), to_unicode=True, utf8_handling="replace")
    index = 0
    try:
        for index, record in enumerate(reader, start=1):
            if record is None:
                error = f"unreadable record: {reader.current_exception!r}"
                yield SourceRecord(record_id=f"#{index}", extra={"error": error})
                continue
            yield _map(record, index, subject_scheme)
    except Exception as error:  # pymarc raises its fatal reader errors from the iterator itself
        raise HarvestError(f"The MARC file is unreadable after record {index}: {error!r}") from error


def _map(record, index: int, subject_scheme: str) -> SourceRecord:
    mapped = marc.to_source_record(record, fallback_id=None, subject_scheme=subject_scheme)
    if not mapped.record_id:
        # No 001: the ISBN is the next most stable id a re-delivered file keeps.
        mapped.record_id = mapped.identifiers.get("isbn") or f"#{index}"
    return mapped


class MarcFileAdapter:
    exhaustive = False

    def __init__(self, source, *, content: Optional[bytes] = None):
        self.source = source
        self.cursor: Optional[str] = None
        self._content = content

    def content(self) -> bytes:
        if self._content is not None:
            return self._content
        if not self.source.upload_path:
            raise HarvestError("No MARC file has been uploaded for this source.")
        storage = get_storage()
        try:
            with storage.open(self.source.upload_path, "rb") as handle:
                self._content = handle.read()
        except (OSError, FileNotFoundError) as error:
            raise HarvestError(f"The uploaded MARC file cannot be read: {error}") from error
        return self._content

    def records(self) -> Iterator[SourceRecord]:
        yield from read_marc(self.content(), subject_scheme=self.source.slug)

    def probe(self) -> dict:
        result = {"identify": None, "metadata_formats": [], "sets": [], "sample": None, "errors": []}
        try:
            result["sample"] = next(self.records(), None)
            result["sample"] = result["sample"].as_dict() if result["sample"] is not None else None
        except HarvestError as error:
            result["errors"].append(str(error))
        return result


NAME = "marc_file"


@registry.register
class MarcFileConnector(Connector):
    name = NAME
    title = "MARC file"
    description = "ISO 2709 or MARCXML file uploaded through `POST harvest/sources/{id}/upload`. Read only."
    needs_upload = True

    def reader(self, source, *, full=False):
        return MarcFileAdapter(source)

    def fetch(self, source, record_id):
        return next((record for record in MarcFileAdapter(source).records() if record.record_id == record_id), None)


__all__ = ["MarcFileAdapter", "MarcFileConnector", "NAME", "is_xml", "read_marc"]
