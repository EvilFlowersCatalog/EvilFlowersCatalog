import abc
import json
import urllib.error
from typing import Literal, Optional
from urllib.request import Request, urlopen

from isbnlib import meta, canonical
from isbnlib.registry import bibformatters


class IntrospectionDriver(abc.ABC):
    @abc.abstractmethod
    def resolve(self, identifier: str) -> Optional[dict]:
        pass


class IsbnDriver(IntrospectionDriver):
    def resolve(self, identifier: str) -> Optional[dict]:
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
            res = urlopen(req, timeout=5)
            data = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError | json.JSONDecodeError:
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
            res = urlopen(req, timeout=5)
        except urllib.error.HTTPError:
            return None

        result["bibtex"] = res.read().decode("utf-8")

        return result


class EntryIntrospectionService:
    def __init__(self, driver: Literal["isbn", "dio"]):
        if driver == "isbn":
            self._driver = IsbnDriver()
        elif driver == "doi":
            self._driver = DoiDriver()
        else:
            raise Exception(f"Invalid IntospectionServiceDriver {driver}")

    def resolve(self, identifier: str) -> Optional[dict]:
        return self._driver.resolve(identifier)
