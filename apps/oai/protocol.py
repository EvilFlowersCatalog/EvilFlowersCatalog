"""IP-033: the OAI-PMH 2.0 request grammar (§3.1, §4) and stateless resumption tokens (§3.5).

`RequestParser` turns GET or POST form arguments into an `OaiRequest` or raises `OaiError` with `badVerb` /
`badArgument`. Values that would not be schema-valid in the echoed `<request>` (a malformed `metadataPrefix`,
`set` or `identifier`) or not XML at all (control characters) are refused here, so no response ever echoes them
and no request can make the anonymous endpoint fail.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Optional

from django.core import signing
from lxml import etree

GRANULARITY = "YYYY-MM-DDThh:mm:ssZ"

BAD_ARGUMENT = "badArgument"
BAD_RESUMPTION_TOKEN = "badResumptionToken"
BAD_VERB = "badVerb"
CANNOT_DISSEMINATE_FORMAT = "cannotDisseminateFormat"
ID_DOES_NOT_EXIST = "idDoesNotExist"
NO_METADATA_FORMATS = "noMetadataFormats"
NO_RECORDS_MATCH = "noRecordsMatch"
NO_SET_HIERARCHY = "noSetHierarchy"

#: The salt of resumption tokens (`django.core.signing`): a token is no other signed value.
TOKEN_SALT = "oai"


class OaiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Grammar:
    required: tuple = ()
    optional: tuple = ()
    exclusive: Optional[str] = None


_LIST = Grammar(required=("metadataPrefix",), optional=("from", "until", "set"), exclusive="resumptionToken")

VERBS = {
    "Identify": Grammar(),
    "ListMetadataFormats": Grammar(optional=("identifier",)),
    "ListSets": Grammar(exclusive="resumptionToken"),
    "GetRecord": Grammar(required=("identifier", "metadataPrefix")),
    "ListIdentifiers": _LIST,
    "ListRecords": _LIST,
}

# The XSD's metadataPrefixType and setSpecType.
_PREFIX = re.compile(r"^[A-Za-z0-9\-_.!~*'()]+$")
_SET_SPEC = re.compile(r"^[A-Za-z0-9\-_.!~*'()]+(:[A-Za-z0-9\-_.!~*'()]+)*$")
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")
# `identifier` is echoed as an xs:anyURI attribute; libxml2 decides what that is, so ask it.
_ANY_URI = etree.XMLSchema(
    etree.XML(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="uri" type="xs:anyURI"/></xs:schema>'
    )
)
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@dataclass
class OaiRequest:
    verb: str
    #: The arguments as sent, without `verb` (echoed as attributes of `<request>`).
    arguments: dict = field(default_factory=dict)
    from_: Optional[datetime] = None
    until: Optional[datetime] = None

    def get(self, name: str) -> Optional[str]:
        return self.arguments.get(name)


def _is_uri(value: str) -> bool:
    element = etree.Element("uri")
    element.text = value
    return _ANY_URI.validate(element)


def format_datestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value: str, *, end_of_day: bool = False) -> tuple[datetime, str]:
    """A `from`/`until` value and its granularity (`day` or `seconds`); §3.3.1 day bounds are inclusive."""
    try:
        if _SECONDS.match(value):
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc), "seconds"
        if _DAY.match(value):
            day = datetime.strptime(value, "%Y-%m-%d").date()
            moment = time(23, 59, 59) if end_of_day else time(0, 0, 0)
            return datetime.combine(day, moment, tzinfo=timezone.utc), "day"
    except ValueError:
        pass
    raise OaiError(BAD_ARGUMENT, f"'{value}' is not a date of granularity YYYY-MM-DD or {GRANULARITY}.")


class RequestParser:
    def parse(self, params) -> OaiRequest:
        """`params`: a `QueryDict` (GET or POST form)."""
        for key in params.keys():
            if any(_XML_ILLEGAL.search(text) for text in (key, *params.getlist(key))):
                code = BAD_VERB if key == "verb" else BAD_ARGUMENT
                raise OaiError(code, "The request contains characters that are not allowed in XML.")
        verbs = params.getlist("verb")
        if not verbs:
            raise OaiError(BAD_VERB, "The verb argument is missing.")
        if len(verbs) > 1:
            raise OaiError(BAD_VERB, "The verb argument is repeated.")
        verb = verbs[0]
        grammar = VERBS.get(verb)
        if grammar is None:
            raise OaiError(BAD_VERB, f"'{verb}' is not an OAI-PMH verb.")

        arguments = {}
        for key in params.keys():
            if key == "verb":
                continue
            values = params.getlist(key)
            if len(values) > 1:
                raise OaiError(BAD_ARGUMENT, f"The argument '{key}' is repeated.")
            arguments[key] = values[0]

        if grammar.exclusive and grammar.exclusive in arguments:
            if len(arguments) > 1:
                raise OaiError(BAD_ARGUMENT, f"'{grammar.exclusive}' is an exclusive argument.")
            if not arguments[grammar.exclusive]:
                raise OaiError(BAD_RESUMPTION_TOKEN, "The resumption token is empty.")
            return OaiRequest(verb=verb, arguments=arguments)

        allowed = set(grammar.required) | set(grammar.optional)
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise OaiError(BAD_ARGUMENT, f"Illegal argument(s) for {verb}: {', '.join(unknown)}.")
        missing = [name for name in grammar.required if not arguments.get(name)]
        if missing:
            raise OaiError(BAD_ARGUMENT, f"Missing required argument(s) for {verb}: {', '.join(missing)}.")

        if "metadataPrefix" in arguments and not _PREFIX.match(arguments["metadataPrefix"]):
            raise OaiError(BAD_ARGUMENT, "The metadataPrefix argument is malformed.")
        if "set" in arguments and not _SET_SPEC.match(arguments["set"]):
            raise OaiError(BAD_ARGUMENT, "The set argument is not a setSpec.")
        if "identifier" in arguments and not (arguments["identifier"] and _is_uri(arguments["identifier"])):
            raise OaiError(BAD_ARGUMENT, "The identifier argument is not a URI.")

        request = OaiRequest(verb=verb, arguments=arguments)
        granularities = set()
        if "from" in arguments:
            request.from_, granularity = parse_date(arguments["from"])
            granularities.add(granularity)
        if "until" in arguments:
            request.until, granularity = parse_date(arguments["until"], end_of_day=True)
            granularities.add(granularity)
        if len(granularities) > 1:
            raise OaiError(BAD_ARGUMENT, "The from and until arguments have different granularities.")
        if request.from_ and request.until and request.from_ > request.until:
            raise OaiError(BAD_ARGUMENT, "The from argument is later than the until argument.")
        return request


# -- resumption tokens ---------------------------------------------------------------------------------------


@dataclass
class ListPosition:
    """Everything the next page of a list needs: no server state, any worker serves it (§3.5)."""

    verb: str
    total: int
    cursor: int
    prefix: Optional[str] = None
    set: Optional[str] = None
    from_: Optional[datetime] = None
    until: Optional[datetime] = None
    #: The last (datestamp, id) served: the keyset to continue after.
    after: Optional[tuple[datetime, int]] = None
    #: ListSets pages by offset (sets are a small, sorted list).
    offset: int = 0


class ResumptionTokenCodec:
    """Signed, compressed, never expiring: re-issuing a token returns the same page (§3.5.1)."""

    def encode(self, position: ListPosition) -> str:
        payload = {
            "v": position.verb,
            "n": position.total,
            "c": position.cursor,
            "p": position.prefix,
            "s": position.set,
            "f": format_datestamp(position.from_) if position.from_ else None,
            "u": format_datestamp(position.until) if position.until else None,
            "a": [format_datestamp(position.after[0]), position.after[1]] if position.after else None,
            "o": position.offset,
        }
        return signing.dumps(payload, salt=TOKEN_SALT, compress=True)

    def decode(self, token: str, verb: str) -> ListPosition:
        try:
            payload = signing.loads(token, salt=TOKEN_SALT)
            if payload["v"] != verb:
                raise ValueError("token of another verb")

            def moment(value):
                return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if value else None

            after = payload.get("a")
            return ListPosition(
                verb=payload["v"],
                total=int(payload["n"]),
                cursor=int(payload["c"]),
                prefix=payload.get("p"),
                set=payload.get("s"),
                from_=moment(payload.get("f")),
                until=moment(payload.get("u")),
                after=(moment(after[0]), int(after[1])) if after else None,
                offset=int(payload.get("o") or 0),
            )
        except (signing.BadSignature, KeyError, TypeError, ValueError, IndexError):
            raise OaiError(BAD_RESUMPTION_TOKEN, "The resumption token is invalid.")


def truncate(moment: datetime) -> datetime:
    """Whole seconds, UTC: the datestamp granularity."""
    return moment.astimezone(timezone.utc).replace(microsecond=0)


__all__ = [
    "GRANULARITY",
    "ListPosition",
    "OaiError",
    "OaiRequest",
    "RequestParser",
    "ResumptionTokenCodec",
    "VERBS",
    "format_datestamp",
    "parse_date",
    "truncate",
]
