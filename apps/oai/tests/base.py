"""Shared fixtures of the IP-033 tests: a harvestable catalog `stu`, a private one, and helpers."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone as django_timezone
from lxml import etree

from apps.core import settings_store
from apps.core.models import (
    Acquisition,
    AuthSource,
    Author,
    Catalog,
    Category,
    Entry,
    EntryAuthor,
    Language,
    User,
)
from apps.oai.models import OaiRecord
from apps.oai.sync import OaiRecordSync

PORTAL = "https://elvira.stuba.sk"
UDC = "http://udcdata.info/"
LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)
SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
    "marc": "http://www.loc.gov/MARC21/slim",
    "id": "http://www.openarchives.org/OAI/2.0/oai-identifier",
}

_schema = None


def schema() -> etree.XMLSchema:
    """OAI-PMH.xsd with oai_dc, MARC21slim and oai-identifier, vendored (offline)."""
    global _schema
    if _schema is None:
        _schema = etree.XMLSchema(etree.parse(str(SCHEMAS / "all.xsd")))
    return _schema


OAI_SETTINGS = dict(
    EVILFLOWERS_OAI_ENABLED=True,
    EVILFLOWERS_OAI_REPOSITORY_IDENTIFIER="evilflowers.elvira.stuba.sk",
    EVILFLOWERS_OAI_REPOSITORY_NAME="Elvira",
    EVILFLOWERS_OAI_PAGE_SIZE=100,
    EVILFLOWERS_BASE_URL="https://evilflowers.elvira.stuba.sk",
    # The default `root@localhost` is no `emailType` of the OAI-PMH schema.
    EVILFLOWERS_CONTACT_EMAIL="elvira@stuba.sk",
    EVILFLOWERS_MARC_ORG_CODE="ELVIRA",
    EVILFLOWERS_EXTERNAL_SYSTEMS=[
        {
            "key": "arl_stu",
            "name": "STU library (ARL)",
            "kind": "ils",
            "marc_org_code": "SK-BA-STU",
            "record_url_template": "https://kis.cvt.stuba.sk/arl-stu/sk/detail-stu_us_cat-{record_id}/",
        }
    ],
)


@override_settings(**OAI_SETTINGS)
class OaiTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.owner = User.objects.create(username="owner", name="O", surname="W", auth_source=source)
        cls.stu = Catalog.objects.create(
            creator=cls.owner, title="STU", url_name="stu", is_public=True, harvestable=True
        )
        cls.private = Catalog.objects.create(creator=cls.owner, title="Private", url_name="private")
        cls.slovak, _ = Language.objects.get_or_create(alpha2="sk", defaults={"name": "Slovak", "alpha3": "slk"})
        if cls.slovak.alpha3 != "slk":
            cls.slovak.alpha3 = "slk"
            cls.slovak.save()
        cls.udc = Category.objects.create(
            creator=cls.owner, catalog=cls.stu, term="621.3", scheme=UDC, label={"sk": "Elektrotechnika"}
        )

    def setUp(self):
        settings_store.invalidate()
        settings_store.update("content.cite_base_url", PORTAL)
        self.addCleanup(settings_store.invalidate)

    # -- builders --------------------------------------------------------------------------------------------

    def entry(self, title="Elektrické stroje", catalog=None, acquisition=True, authors=(("Novák", "Ján"),), **fields):
        config = fields.pop("config", {})
        entry = Entry.objects.create(
            creator=self.owner,
            catalog=catalog or self.stu,
            title=title,
            language=fields.pop("language", self.slovak),
            config=config,
            **fields,
        )
        for position, (surname, name) in enumerate(authors):
            author = Author.objects.create(catalog=entry.catalog, surname=surname, name=name)
            EntryAuthor.objects.create(entry=entry, author=author, position=position)
        if acquisition:
            Acquisition.objects.create(entry=entry, mime=Acquisition.AcquisitionMIME.PDF)
        return entry

    def sync(self, *entries, **kwargs):
        return OaiRecordSync(**kwargs).sync([getattr(entry, "pk", entry) for entry in entries])

    def record(self, entry) -> OaiRecord:
        return OaiRecord.objects.get(entry_id=getattr(entry, "pk", entry))

    def backdate(self, entry, when: datetime = LONG_AGO):
        OaiRecord.objects.filter(entry_id=getattr(entry, "pk", entry)).update(datestamp=when)

    def moved(self, entry) -> bool:
        return self.record(entry).datestamp > LONG_AGO

    def run_signals(self):
        """Run on-commit callbacks, with `sync_entries.delay` executed in-process."""
        from apps.oai.tasks import sync_entries

        test = self

        class _Runner:
            def __enter__(self):
                # Changes made before this block belong to a batch the test transaction never commits; collect
                # into a fresh one that the capture below runs.
                from django.db import connection

                connection._oai_batch = None
                self.delay = patch("apps.oai.tasks.sync_entries.delay", side_effect=lambda ids: sync_entries(ids))
                self.mock = self.delay.start()
                self.capture = test.captureOnCommitCallbacks(execute=True)
                self.capture.__enter__()
                return self.mock

            def __exit__(self, *exc):
                try:
                    self.capture.__exit__(*exc)
                finally:
                    self.delay.stop()
                return False

        return _Runner()

    @staticmethod
    def now():
        return django_timezone.now()
