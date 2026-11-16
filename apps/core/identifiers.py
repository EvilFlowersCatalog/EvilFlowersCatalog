"""Which keys `Entry.identifiers` may hold (IP-033 Q8): bibliographic identifiers plus one key per external
system (a library catalogue, a partner's repository) that an entry can be linked to.

The systems are configuration, `EVILFLOWERS_EXTERNAL_SYSTEMS` (JSON list), so a new partner needs no code:

    [{"key": "arl_stu", "name": "STU library (ARL)", "kind": "ils", "marc_org_code": "SK-BA-STU",
      "record_url_template": "https://kis.cvt.stuba.sk/arl-stu/sk/detail-stu_us_cat-{record_id}/"}]

The keys are read at call time, never frozen into a model field or migration.
"""

from dataclasses import dataclass
from typing import Optional

from django.conf import settings

#: Keys owned by the Dataverse sync, not by a configured system.
DATAVERSE_KEYS = ("dataverse_pid", "dataverse_dataset_id")


@dataclass(frozen=True)
class ExternalSystem:
    key: str
    name: str
    kind: str = "ils"
    marc_org_code: Optional[str] = None
    record_url_template: Optional[str] = None

    def record_url(self, record_id: str) -> Optional[str]:
        if not self.record_url_template or not record_id:
            return None
        return self.record_url_template.format(record_id=record_id)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "kind": self.kind,
            "marc_org_code": self.marc_org_code,
            "record_url_template": self.record_url_template,
        }


def external_systems() -> list[ExternalSystem]:
    systems = []
    for raw in getattr(settings, "EVILFLOWERS_EXTERNAL_SYSTEMS", None) or []:
        if not isinstance(raw, dict) or not raw.get("key"):
            continue
        systems.append(
            ExternalSystem(
                key=str(raw["key"]),
                name=str(raw.get("name") or raw["key"]),
                kind=str(raw.get("kind") or "ils"),
                marc_org_code=raw.get("marc_org_code") or None,
                record_url_template=raw.get("record_url_template") or None,
            )
        )
    return systems


def external_system(key: str) -> Optional[ExternalSystem]:
    return next((system for system in external_systems() if system.key == key), None)


def allowed_identifier_keys() -> set[str]:
    return set(settings.EVILFLOWERS_IDENTIFIERS) | {system.key for system in external_systems()}


__all__ = [
    "DATAVERSE_KEYS",
    "ExternalSystem",
    "allowed_identifier_keys",
    "external_system",
    "external_systems",
]
