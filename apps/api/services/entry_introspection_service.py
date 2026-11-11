import abc
import json
import urllib.error
from typing import Literal, Optional
from urllib.request import Request, urlopen

from urllib.parse import urlsplit

from isbnlib import meta, canonical
from isbnlib.registry import bibformatters

from evil_flowers_catalog import tracing

try:
    from opentelemetry.util.http import redact_url
except ImportError:  # pragma: no cover - built without the `otel` group: nothing records spans anyway
    # The only URLs here are `https://doi.org/<doi>`, which carry no credentials.
    def redact_url(url: str) -> str:
        return url


def _fetch(req: Request) -> bytes:
    """`urlopen` as an HTTP CLIENT span; `requests` is instrumented, `urllib` is not (IP-029)."""
    host = urlsplit(req.full_url).hostname
    with tracing.client_span(
        req.get_method(),
        system="http",
        server_address=host,
        server_port=443,
        # The same redaction the `requests` instrumentation applies to its `url.full`.
        attributes={"http.request.method": req.get_method(), "url.full": redact_url(req.full_url)},
        # A DOI the registry does not know is an answer, not a fault.
        expected=(urllib.error.HTTPError,),
    ) as span:
        res = urlopen(req, timeout=5)
        span.set_attribute("http.response.status_code", res.status)
        return res.read()


class IntrospectionDriver(abc.ABC):
    @abc.abstractmethod
    def resolve(self, identifier: str) -> Optional[dict]:
        pass


class IsbnDriver(IntrospectionDriver):
    def resolve(self, identifier: str) -> Optional[dict]:
        # isbnlib queries its metadata providers itself; the step at least shows how long they took.
        with tracing.client_span("isbnlib meta", system="isbnlib", server_address=None):
            data = meta(canonical(identifier))

        result = {
            "publisher": data.get("Publisher"),
            "doi": None,
            "authors": [],
            "year": data.get("Year"),
            "language": data.get("Language"),
            "bibtex": bibformatters["bibtex"](data),
        }

        for author in data.get("Authors", []):
            bits = author.split(" ")
            result["authors"].append({"name": bits[0], "surname": " ".join(bits[1:])})

        return result


class DoiDriver(IntrospectionDriver):
    def resolve(self, identifier: str) -> Optional[dict]:
        req = Request(
            url=f"https://doi.org/{identifier}",
            headers={"Accept": "application/vnd.citationstyles.csl+json"},
        )

        try:
            data = json.loads(_fetch(req).decode("utf-8"))
        except (urllib.error.HTTPError, json.JSONDecodeError):
            return None

        result = {
            "publisher": data.get("publisher"),
            "doi": data.get("DOI"),
            "authors": [{"name": i["given"], "surname": i["family"]} for i in data.get("author")],
            "title": data.get("title"),
        }

        req = Request(
            url=f"https://doi.org/{identifier}",
            headers={"Accept": "application/x-bibtex"},
        )

        try:
            result["bibtex"] = _fetch(req).decode("utf-8")
        except urllib.error.HTTPError:
            return None

        return result


class EntryIntrospectionService:
    def __init__(self, driver: Literal["isbn", "doi"]):
        if driver == "isbn":
            self._driver = IsbnDriver()
        elif driver == "doi":
            self._driver = DoiDriver()
        else:
            raise Exception(f"Invalid IntospectionServiceDriver {driver}")

    def resolve(self, identifier: str) -> Optional[dict]:
        with tracing.span("introspection.resolve", {"evilflowers.introspection.driver": type(self._driver).__name__}):
            return self._driver.resolve(identifier)
