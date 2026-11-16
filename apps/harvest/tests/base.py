"""Shared set-up of the harvest tests: a catalog with entries to match, an ARL-like source, recorded OAI pages.

The fixtures under `fixtures/` are synthetic: written by hand in the shape ARL's and a DSpace's OAI-PMH answer
(MARCXML in `marc21`, unqualified DC in `oai_dc`), not captured from a live server.
"""

from pathlib import Path
from unittest.mock import MagicMock

from django.test import TestCase, override_settings

from apps.core.models import AuthSource, Catalog, Entry, Language, User, UserCatalog
from apps.harvest.models import HarvestSource

FIXTURES = Path(__file__).parent / "fixtures"

SYSTEMS = [
    {
        "key": "arl_stu",
        "name": "STU library (ARL)",
        "kind": "ils",
        "record_url_template": "https://kis.example.org/detail-{record_id}/",
    },
    {"key": "partner", "name": "Partner library", "kind": "ils"},
]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeSession:
    """Answers OAI-PMH requests from fixtures by `verb` (and `resumptionToken`); records every call."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[dict] = []
        self.headers: dict = {}

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append(params)
        key = params.get("resumptionToken") or params.get("verb")
        if key not in self.pages and params.get("verb") == "ListRecords":
            key = "ListRecords"
        response = MagicMock()
        response.status_code = 200 if key in self.pages else 404
        response.content = fixture(self.pages[key]) if key in self.pages else b""
        return response


ARL_PAGES = {
    "Identify": "identify.xml",
    "ListRecords": "marc21_page1.xml",
    "page-2": "marc21_page2.xml",
    "ListMetadataFormats": "formats_sets.xml",
    "ListSets": "formats_sets.xml",
}


@override_settings(EVILFLOWERS_EXTERNAL_SYSTEMS=SYSTEMS)
class HarvestTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.superuser = cls._user("root", is_superuser=True)
        cls.manager = cls._user("manager")
        cls.outsider = cls._user("outsider")
        cls.catalog = Catalog.objects.create(creator=cls.superuser, title="STU", url_name="stu")
        cls.other_catalog = Catalog.objects.create(creator=cls.outsider, title="Other", url_name="other")
        UserCatalog.objects.create(user=cls.manager, catalog=cls.catalog, mode=UserCatalog.Mode.MANAGE)
        UserCatalog.objects.create(user=cls.outsider, catalog=cls.other_catalog, mode=UserCatalog.Mode.MANAGE)
        cls.sk, _ = Language.objects.get_or_create(alpha2="sk", defaults={"alpha3": "slk", "name": "Slovak"})
        cls.en, _ = Language.objects.get_or_create(alpha2="en", defaults={"alpha3": "eng", "name": "English"})
        Language.objects.get_or_create(alpha2="cs", defaults={"alpha3": "ces", "name": "Czech"})

    @classmethod
    def _user(cls, username: str, **kwargs) -> User:
        user = User(username=username, name=username.title(), surname="T", auth_source=cls.auth_source, **kwargs)
        user.set_password("password-1234")
        user.save()
        return user

    def entry(self, title: str, **kwargs) -> Entry:
        kwargs.setdefault("language", self.sk)
        return Entry.objects.create(creator=self.superuser, catalog=self.catalog, title=title, **kwargs)

    def source(self, **kwargs) -> HarvestSource:
        values = {
            "slug": "arl-stu",
            "name": "ARL STU",
            "adapter": "oai_pmh",
            "base_url": "https://kis.example.org/oai",
            "metadata_prefix": "marc21",
            "system_key": "arl_stu",
            "target_catalog": self.catalog,
            "mode": HarvestSource.Mode.LINK_ONLY,
            "enabled": True,
            "creator": self.superuser,
        }
        values.update(kwargs)
        return HarvestSource.objects.create(**values)


__all__ = ["ARL_PAGES", "FakeSession", "HarvestTestCase", "SYSTEMS", "fixture"]
