"""IP-033: the six OAI-PMH 2.0 verbs over `oai_records`.

Lists are a keyset walk on `(datestamp, id)` between `from` and a frozen `until` (`responseDate` when absent),
`page_size` records per page. The token carries the arguments and the last `(datestamp, id)`, so pages need
no server state; a record changed during the walk moves past `until` and comes with the next harvest (§5).
Errors are OAI XML with HTTP 200.
"""

from datetime import datetime, timezone
from typing import Optional

from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.utils import timezone as django_timezone
from lxml import etree

from apps.oai import protocol
from apps.oai.crosswalks import CROSSWALKS, XSI
from apps.oai.models import OaiRecord
from apps.oai.projection import label_of, oai_identifier, parse_identifier
from apps.oai.protocol import ListPosition, OaiError, OaiRequest, format_datestamp
from evil_flowers_catalog import tracing

OAI = "http://www.openarchives.org/OAI/2.0/"
OAI_SCHEMA = "http://www.openarchives.org/OAI/2.0/OAI-PMH.xsd"
OAI_IDENTIFIER = "http://www.openarchives.org/OAI/2.0/oai-identifier"
OAI_IDENTIFIER_SCHEMA = "http://www.openarchives.org/OAI/2.0/oai-identifier.xsd"

#: A sample entry id for the `oai-identifier` description.
SAMPLE_ID = "5e6b7f7c-3f1a-4c55-9a8e-0d3e2a1b4c6d"


def _tag(name: str) -> str:
    return f"{{{OAI}}}{name}"


def _sub(parent, name: str, value: Optional[str] = None, **attributes) -> etree._Element:
    element = etree.SubElement(parent, _tag(name), **attributes)
    if value is not None:
        element.text = value
    return element


class OaiRepository:
    def __init__(self, base_url: Optional[str] = None, page_size: Optional[int] = None):
        self.base_url = base_url or f"{settings.EVILFLOWERS_BASE_URL.rstrip('/')}/oai"
        self.page_size = page_size or settings.EVILFLOWERS_OAI_PAGE_SIZE
        self.parser = protocol.RequestParser()
        self.tokens = protocol.ResumptionTokenCodec()

    # -- envelope --------------------------------------------------------------------------------------------

    def respond(self, params) -> bytes:
        """The complete response document for GET or POST form arguments."""
        response_date = protocol.truncate(django_timezone.now())
        root = etree.Element(_tag("OAI-PMH"), nsmap={None: OAI, "xsi": XSI})
        root.set(f"{{{XSI}}}schemaLocation", f"{OAI} {OAI_SCHEMA}")
        _sub(root, "responseDate", format_datestamp(response_date))
        request_element = _sub(root, "request", self.base_url)
        try:
            request = self.parser.parse(params)
        except OaiError as error:
            # §3.2: after badVerb or badArgument, the request element carries no attributes.
            self._error(root, error)
            return self._serialize(root)

        request_element.attrib.update({"verb": request.verb, **request.arguments})
        with tracing.span("oai.request", {"oai.verb": request.verb}) as span:
            try:
                body, attributes = getattr(self, request.verb)(request, response_date)
                root.append(body)
                for key, value in attributes.items():
                    span.set_attribute(key, value)
            except OaiError as error:
                span.set_attribute("oai.error", error.code)
                self._error(root, error)
        return self._serialize(root)

    @staticmethod
    def _error(root, error: OaiError):
        _sub(root, "error", error.message, code=error.code)

    @staticmethod
    def _serialize(root) -> bytes:
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")

    # -- verbs -----------------------------------------------------------------------------------------------

    def Identify(self, request: OaiRequest, response_date: datetime):
        body = etree.Element(_tag("Identify"))
        _sub(body, "repositoryName", settings.EVILFLOWERS_OAI_REPOSITORY_NAME)
        _sub(body, "baseURL", self.base_url)
        _sub(body, "protocolVersion", "2.0")
        _sub(body, "adminEmail", settings.EVILFLOWERS_CONTACT_EMAIL)
        earliest = OaiRecord.objects.order_by("datestamp").values_list("datestamp", flat=True).first()
        # Nothing harvested yet: the response date is a lower bound no record can precede.
        _sub(body, "earliestDatestamp", format_datestamp(earliest or response_date))
        _sub(body, "deletedRecord", "persistent")
        _sub(body, "granularity", protocol.GRANULARITY)

        description = _sub(body, "description")
        identifier = etree.SubElement(
            description, f"{{{OAI_IDENTIFIER}}}oai-identifier", nsmap={None: OAI_IDENTIFIER, "xsi": XSI}
        )
        identifier.set(f"{{{XSI}}}schemaLocation", f"{OAI_IDENTIFIER} {OAI_IDENTIFIER_SCHEMA}")
        for name, value in (
            ("scheme", "oai"),
            ("repositoryIdentifier", settings.EVILFLOWERS_OAI_REPOSITORY_IDENTIFIER),
            ("delimiter", ":"),
            ("sampleIdentifier", oai_identifier(SAMPLE_ID)),
        ):
            etree.SubElement(identifier, f"{{{OAI_IDENTIFIER}}}{name}").text = value
        return body, {}

    def ListMetadataFormats(self, request: OaiRequest, response_date: datetime):
        if request.get("identifier"):
            # Every record is disseminated in every format (a deleted one as a header only).
            self._record(request.get("identifier"))
        body = etree.Element(_tag("ListMetadataFormats"))
        for crosswalk in CROSSWALKS.values():
            item = _sub(body, "metadataFormat")
            _sub(item, "metadataPrefix", crosswalk.prefix)
            _sub(item, "schema", crosswalk.schema)
            _sub(item, "metadataNamespace", crosswalk.namespace)
        return body, {}

    def ListSets(self, request: OaiRequest, response_date: datetime):
        token = request.get("resumptionToken")
        sets = self._sets()
        if token:
            position = self.tokens.decode(token, request.verb)
        else:
            if not sets:
                raise OaiError(protocol.NO_SET_HIERARCHY, "No catalog of this repository is harvestable.")
            position = ListPosition(verb=request.verb, total=len(sets), cursor=0, offset=0)
        page = sets[position.offset : position.offset + self.page_size]
        if not page:
            raise OaiError(protocol.BAD_RESUMPTION_TOKEN, "The resumption token is past the end of the list.")

        body = etree.Element(_tag("ListSets"))
        for spec, name in page:
            item = _sub(body, "set")
            _sub(item, "setSpec", spec)
            _sub(item, "setName", name)
        following = position.offset + len(page)
        if following < len(sets):
            next_position = ListPosition(verb=request.verb, total=len(sets), cursor=following, offset=following)
            self._token(body, self.tokens.encode(next_position), len(sets), position.offset)
        elif token:
            self._token(body, "", len(sets), position.offset)
        return body, {"oai.sets": len(page)}

    def GetRecord(self, request: OaiRequest, response_date: datetime):
        crosswalk = self._crosswalk(request.get("metadataPrefix"))
        record = self._record(request.get("identifier"))
        body = etree.Element(_tag("GetRecord"))
        body.append(self._record_element(record, crosswalk))
        return body, {"oai.prefix": crosswalk.prefix}

    def ListIdentifiers(self, request: OaiRequest, response_date: datetime):
        return self._list(request, response_date, with_metadata=False)

    def ListRecords(self, request: OaiRequest, response_date: datetime):
        return self._list(request, response_date, with_metadata=True)

    # -- lists -----------------------------------------------------------------------------------------------

    def _list(self, request: OaiRequest, response_date: datetime, with_metadata: bool):
        token = request.get("resumptionToken")
        if token:
            position = self.tokens.decode(token, request.verb)
            crosswalk = CROSSWALKS.get(position.prefix)
            if crosswalk is None:
                raise OaiError(protocol.BAD_RESUMPTION_TOKEN, "The resumption token names an unknown format.")
        else:
            crosswalk = self._crosswalk(request.get("metadataPrefix"))
            position = ListPosition(
                verb=request.verb,
                total=0,
                cursor=0,
                prefix=crosswalk.prefix,
                set=request.get("set"),
                from_=request.from_,
                until=request.until or response_date,
            )

        records = OaiRecord.objects.all()
        if position.set:
            # A record carries its catalog's spec and each category's, so containment covers the hierarchy.
            records = records.filter(set_specs__contains=[position.set])
        if position.from_:
            records = records.filter(datestamp__gte=position.from_)
        records = records.filter(datestamp__lte=position.until)
        if not token:
            position.total = records.count()
        if position.after:
            datestamp, last_id = position.after
            records = records.filter(Q(datestamp__gt=datestamp) | Q(datestamp=datestamp, id__gt=last_id))
        if not with_metadata:
            records = records.defer("projection")
        rows = list(records.order_by("datestamp", "id")[: self.page_size + 1])
        if not rows:
            # Also a page whose remaining records all changed during the walk: the list ends here.
            raise OaiError(protocol.NO_RECORDS_MATCH, "No record matches the arguments.")

        page = rows[: self.page_size]
        body = etree.Element(_tag(request.verb))
        for record in page:
            body.append(self._record_element(record, crosswalk) if with_metadata else self._header(record))

        total = max(position.total, position.cursor + len(page))
        if len(rows) > self.page_size:
            last = page[-1]
            following = ListPosition(
                verb=request.verb,
                total=total,
                cursor=position.cursor + len(page),
                prefix=position.prefix,
                set=position.set,
                from_=position.from_,
                until=position.until,
                after=(last.datestamp, last.id),
            )
            self._token(body, self.tokens.encode(following), total, position.cursor)
        elif token:
            self._token(body, "", total, position.cursor)
        attributes = {"oai.prefix": crosswalk.prefix, "oai.records": len(page)}
        if position.set:
            attributes["oai.set"] = position.set
        return body, attributes

    @staticmethod
    def _token(body, value: str, total: int, cursor: int):
        _sub(body, "resumptionToken", value, completeListSize=str(total), cursor=str(cursor))

    # -- records ---------------------------------------------------------------------------------------------

    @staticmethod
    def _crosswalk(prefix: Optional[str]):
        crosswalk = CROSSWALKS.get(prefix or "")
        if crosswalk is None:
            raise OaiError(protocol.CANNOT_DISSEMINATE_FORMAT, f"The metadata format '{prefix}' is not supported.")
        return crosswalk

    @staticmethod
    def _record(identifier: Optional[str]) -> OaiRecord:
        entry_id = parse_identifier(identifier)
        record = OaiRecord.objects.filter(entry_id=entry_id).first() if entry_id else None
        if record is None:
            raise OaiError(protocol.ID_DOES_NOT_EXIST, f"'{identifier}' is unknown in this repository.")
        return record

    @staticmethod
    def _header(record: OaiRecord) -> etree._Element:
        header = etree.Element(_tag("header"))
        if record.deleted:
            header.set("status", "deleted")
        _sub(header, "identifier", oai_identifier(record.entry_id))
        _sub(header, "datestamp", format_datestamp(record.datestamp))
        for spec in record.set_specs or []:
            _sub(header, "setSpec", spec)
        return header

    def _record_element(self, record: OaiRecord, crosswalk) -> etree._Element:
        element = etree.Element(_tag("record"))
        element.append(self._header(record))
        if not record.deleted and record.projection:
            metadata = _sub(element, "metadata")
            metadata.append(crosswalk.render(record.projection, record.datestamp))
        return element

    # -- sets ------------------------------------------------------------------------------------------------

    @staticmethod
    def _sets() -> list[tuple[str, str]]:
        """Harvestable catalogs, and the categories live records are filed under, sorted by setSpec."""
        from apps.core.models import Catalog, Category

        sets = {
            url_name: title
            for url_name, title in Catalog.objects.filter(harvestable=True).values_list("url_name", "title")
        }
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT spec FROM oai_records, unnest(set_specs) AS spec "
                "WHERE NOT deleted AND position(':' in spec) > 0"
            )
            category_specs = [row[0] for row in cursor.fetchall()]
        by_id = {}
        for spec in category_specs:
            by_id.setdefault(spec.split(":", 1)[1], []).append(spec)
        categories = Category.objects.filter(pk__in=[key for key in by_id if _is_uuid(key)])
        for category in categories:
            label = label_of(category.label)
            code = f"UDC {category.term}" if category.scheme == "http://udcdata.info/" else None
            if label and code:
                name = f"{label} ({code})"
            else:
                name = label or code or category.term
            for spec in by_id[str(category.pk)]:
                sets[spec] = name
        return sorted(sets.items())


def _is_uuid(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


__all__ = ["OaiRepository"]
