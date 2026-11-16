"""An in-memory writing connector for the two-way sync tests: no system, no network."""

import copy
from datetime import datetime, timedelta, timezone

from apps.harvest.contract import Connector, PushResult, SourceRecord, StaleVersion

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


class FakeRemote:
    def __init__(self):
        self.records: dict[str, SourceRecord] = {}
        self.feed: list[SourceRecord] = []
        self.clock = T0
        self.stale_times = 0
        self.pushes: list[tuple] = []
        self.created = 0

    def tick(self, minutes: int = 1) -> str:
        self.clock += timedelta(minutes=minutes)
        return self.clock.isoformat()

    def put(self, record: SourceRecord, *, at: datetime = None) -> SourceRecord:
        record = copy.deepcopy(record)
        record.version = (at or self.clock).isoformat() if at else self.tick()
        self.records[record.record_id] = record
        return record


class FakeConnector(Connector):
    name = "fake"
    title = "Fake repository"
    description = "In-memory, for tests."
    system_keys = ("fake_id",)
    config_schema = {"type": "object", "properties": {"collection": {"type": "string"}}}
    remote = FakeRemote()

    class Reader:
        exhaustive = False

        def __init__(self, records):
            self._records = records
            self.cursor = None

        def records(self):
            yield from self._records
            self.cursor = "after-feed"

        def probe(self):
            return {"identify": None, "metadata_formats": [], "sets": [], "sample": None, "errors": []}

    def reader(self, source, *, full=False):
        return self.Reader(list(self.remote.feed))

    def fetch(self, source, record_id):
        record = self.remote.records.get(record_id)
        return copy.deepcopy(record) if record is not None else None

    def push(self, source, record_id, record, expected_version):
        self.remote.pushes.append((record_id, expected_version))
        if self.remote.stale_times > 0:
            self.remote.stale_times -= 1
            # Someone else edited it meanwhile.
            current = self.remote.records[record_id]
            current.version = self.remote.tick()
            raise StaleVersion("changed meanwhile")
        if record_id is None:
            self.remote.created += 1
            record_id = f"F-{self.remote.created}"
        elif self.remote.records[record_id].version != expected_version:
            raise StaleVersion("version mismatch")
        stored = copy.deepcopy(record)
        stored.record_id = record_id
        stored.version = self.remote.tick()
        self.remote.records[record_id] = stored
        return PushResult(record_id=record_id, version=stored.version, identifiers={"fake_internal": f"#{record_id}"})

    def validate(self, source, record):
        return [] if record.title else ["no title"]

    def record_url(self, source, record_id):
        return f"https://fake.example.org/records/{record_id}"


class ReadOnlyConnector(Connector):
    name = "readonly"
    title = "Read-only repository"

    def reader(self, source, *, full=False):
        return FakeConnector.Reader([])

    def fetch(self, source, record_id):
        return None
