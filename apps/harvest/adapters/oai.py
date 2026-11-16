"""OAI-PMH 2.0 harvester (IP-034): `Identify` first, then incremental `ListRecords` with resumption tokens.

Formats: `marc21` / `oai_marcxml` / `marcxml` (MARCXML through `apps.harvest.adapters.marc`) and `oai_dc`. A live
record's id is its MARC `001` (the catalogue's own record number, what `identifiers[system_key]` holds); a deleted
header carries only the OAI identifier, so every record keeps it in `extra["oai_identifier"]` and the runner
resolves deletions through it.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

import requests
from django.conf import settings

from apps.harvest.adapters import dc, marc
from apps.harvest.adapters.base import USER_AGENT, HarvestError, child, children, local, parse_xml, text
from apps.harvest.adapters.registry import Connector, registry
from apps.harvest.records import SourceRecord

logger = logging.getLogger(__name__)

MARC_PREFIXES = ("marc21", "oai_marcxml", "marcxml", "marc")


class OaiPmhAdapter:
    exhaustive = False

    def __init__(self, source, *, full: bool = False, session: Optional[requests.Session] = None):
        self.source = source
        self.full = full
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.cursor: Optional[str] = None
        self.identify_info: Optional[dict] = None
        # A full harvest lists every live record; a set-restricted one only that set's, which is still all
        # the source has for us.
        self.exhaustive = full

    # -- protocol ----------------------------------------------------------------------------------------

    def request(self, **params):
        try:
            response = self.session.get(
                self.source.base_url, params=params, timeout=settings.EVILFLOWERS_HARVEST_HTTP_TIMEOUT
            )
        except requests.RequestException as error:
            raise HarvestError(f"{params.get('verb')}: {error}") from error
        if response.status_code != 200:
            raise HarvestError(f"{params.get('verb')}: HTTP {response.status_code}")
        root = parse_xml(response.content)
        if local(root) != "OAI-PMH":
            raise HarvestError(f"{params.get('verb')}: not an OAI-PMH response")
        return root

    @staticmethod
    def errors(root) -> list[tuple[str, str]]:
        return [(item.get("code") or "", text(item) or "") for item in children(root, "error")]

    def identify(self) -> dict:
        root = self.request(verb="Identify")
        problems = self.errors(root)
        if problems:
            raise HarvestError("Identify: " + "; ".join(f"{code}: {message}" for code, message in problems))
        node = child(root, "Identify")
        info = {
            name: text(child(node, name))
            for name in (
                "repositoryName",
                "baseURL",
                "protocolVersion",
                "earliestDatestamp",
                "deletedRecord",
                "granularity",
            )
        }
        info["adminEmail"] = None  # never stored or shown: personal data of the source's operator
        self.identify_info = info
        return info

    def metadata_formats(self) -> list[dict]:
        root = self.request(verb="ListMetadataFormats")
        node = child(root, "ListMetadataFormats")
        if node is None:
            return []
        return [
            {
                "prefix": text(child(item, "metadataPrefix")),
                "schema": text(child(item, "schema")),
                "namespace": text(child(item, "metadataNamespace")),
            }
            for item in children(node, "metadataFormat")
        ]

    def sets(self) -> list[dict]:
        """The first page of `ListSets` (enough for choosing one); a repository without sets answers an error."""
        root = self.request(verb="ListSets")
        node = child(root, "ListSets")
        if node is None:
            return []
        return [
            {"spec": text(child(item, "setSpec")), "name": text(child(item, "setName"))}
            for item in children(node, "set")
        ]

    def _from(self) -> Optional[str]:
        if self.full or not self.source.cursor:
            return None
        granularity = (self.identify_info or {}).get("granularity") or "YYYY-MM-DDThh:mm:ssZ"
        return self.source.cursor[:10] if granularity == "YYYY-MM-DD" else self.source.cursor

    def pages(self, *, limit: Optional[int] = None) -> Iterator:
        """`ListRecords` pages; a resumption request sends the token only (OAI-PMH §3.5)."""
        params = {"verb": "ListRecords", "metadataPrefix": self.source.metadata_prefix}
        if self.source.set_spec:
            params["set"] = self.source.set_spec
        since = self._from()
        if since:
            params["from"] = since
        response_date = None
        count = 0
        while True:
            root = self.request(**params)
            response_date = response_date or text(child(root, "responseDate"))
            problems = self.errors(root)
            if problems:
                if any(code == "noRecordsMatch" for code, _message in problems):
                    break
                raise HarvestError("ListRecords: " + "; ".join(f"{code}: {message}" for code, message in problems))
            node = child(root, "ListRecords")
            if node is None:
                break
            yield node
            count += 1
            token = text(child(node, "resumptionToken"))
            if not token or (limit is not None and count >= limit):
                break
            params = {"verb": "ListRecords", "resumptionToken": token}
        self.cursor = response_date

    def parse(self, record_node) -> SourceRecord:
        header = child(record_node, "header")
        oai_identifier = text(child(header, "identifier")) or ""
        datestamp = text(child(header, "datestamp"))
        if header is not None and header.get("status") == "deleted":
            return SourceRecord(
                record_id=oai_identifier, deleted=True, datestamp=datestamp, extra={"oai_identifier": oai_identifier}
            )
        metadata = child(record_node, "metadata")
        payload = (
            next((item for item in metadata if isinstance(item.tag, str)), None) if metadata is not None else None
        )
        if payload is None:
            raise HarvestError(f"{oai_identifier}: record without metadata")
        if local(payload) == "dc":
            record = dc.to_source_record(payload, record_id=oai_identifier, subject_scheme=self.source.slug)
        else:
            element = next(marc.records_in(payload), None)
            if element is None:
                raise HarvestError(f"{oai_identifier}: no MARC record in the metadata")
            record = marc.to_source_record(
                marc.record_from_element(element), fallback_id=oai_identifier, subject_scheme=self.source.slug
            )
        record.datestamp = datestamp
        record.extra["oai_identifier"] = oai_identifier
        return record

    def records(self) -> Iterator[SourceRecord]:
        self.identify()
        for page in self.pages():
            for item in children(page, "record"):
                try:
                    yield self.parse(item)
                except HarvestError as error:
                    # One unreadable record must not end a 20,000-record harvest; it is reported as failed.
                    header = child(item, "header")
                    identifier = text(child(header, "identifier")) if header is not None else None
                    yield SourceRecord(
                        record_id=identifier or "unknown",
                        extra={"oai_identifier": identifier, "error": str(error)},
                    )

    def probe(self) -> dict:
        """`POST harvest/sources/{id}/test`: Identify, formats, sets, one sample record. Writes nothing."""
        result = {"identify": self.identify(), "metadata_formats": [], "sets": [], "sample": None, "errors": []}
        for key, call in (("metadata_formats", self.metadata_formats), ("sets", self.sets)):
            try:
                result[key] = call()
            except HarvestError as error:
                result["errors"].append(str(error))
        saved, self.full = self.full, True
        try:
            for page in self.pages(limit=1):
                record = next(iter(children(page, "record")), None)
                if record is not None:
                    result["sample"] = self.parse(record).as_dict()
        except HarvestError as error:
            result["errors"].append(str(error))
        finally:
            self.full = saved
            self.cursor = None
        return result

    def get_record(self, identifier: str) -> Optional[SourceRecord]:
        """`GetRecord` by OAI identifier; None when the repository does not know it."""
        root = self.request(verb="GetRecord", identifier=identifier, metadataPrefix=self.source.metadata_prefix)
        problems = self.errors(root)
        if any(code == "idDoesNotExist" for code, _message in problems):
            return None
        if problems:
            raise HarvestError("GetRecord: " + "; ".join(f"{code}: {message}" for code, message in problems))
        node = child(root, "GetRecord")
        record = child(node, "record") if node is not None else None
        return self.parse(record) if record is not None else None


NAME = "oai_pmh"


@registry.register
class OaiPmhConnector(Connector):
    name = NAME
    title = "OAI-PMH"
    description = "OAI-PMH 2.0 repository or library catalogue (marc21, oai_marcxml, oai_dc). Read only."
    needs_base_url = True

    def reader(self, source, *, full=False):
        return OaiPmhAdapter(source, full=full)

    def fetch(self, source, record_id):
        """The record id is usually MARC 001; the mirror row knows its OAI identifier."""
        from apps.harvest.models import HarvestRecord

        row = HarvestRecord.objects.filter(source=source, record_id=record_id, dry_run=False).first()
        identifier = ((row.source_record or {}).get("extra") or {}).get("oai_identifier") if row else None
        return OaiPmhAdapter(source).get_record(identifier or record_id)


__all__ = ["MARC_PREFIXES", "NAME", "OaiPmhAdapter", "OaiPmhConnector"]
