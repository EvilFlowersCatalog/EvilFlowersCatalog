"""Three-way merge per field (IP-034 Q10): per-field last write wins against the last-synced state. Pure: no
Django, no I/O, so the rules are tested on plain dicts.

For each field, comparing canonical values (`apps.harvest.projection`):

- equal on both sides now               → nothing; that value is the new base;
- only one side differs from the base   → that side's value goes to the other;
- both differ from the base (and each other) → the side whose record changed later wins (a tie goes to the
  remote, where the edit was published); the losing value is reported as a conflict;
- no base for the field (first sync of an existing link, a newly synced field) never clears data: an empty
  side takes the other's value; two different non-empty values are decided as a conflict.

The loop guard follows: after a push both sides hold the merged values and the base is set to them, so the
remote's own callback (its webhook after our write) finds every field equal and nothing is written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

LOCAL = "local"
REMOTE = "remote"


@dataclass(frozen=True)
class Conflict:
    field: str
    local_value: Any
    remote_value: Any
    winner: str


@dataclass
class MergeResult:
    #: Every field's value once both sides agree.
    merged: dict = field(default_factory=dict)
    #: Remote values the entry must take.
    to_local: dict = field(default_factory=dict)
    #: Local values the remote must take.
    to_remote: dict = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.to_local or self.to_remote)


def empty(value: Any) -> bool:
    return value in (None, "", [], {})


def later(local_at: Optional[datetime], remote_at: Optional[datetime]) -> str:
    if local_at is not None and (remote_at is None or local_at > remote_at):
        return LOCAL
    return REMOTE


def merge(
    base: Optional[Mapping[str, Any]],
    local: Mapping[str, Any],
    remote: Mapping[str, Any],
    local_at: Optional[datetime],
    remote_at: Optional[datetime],
    fields: Optional[Iterable[str]] = None,
) -> MergeResult:
    """Merge `local` and `remote` against `base` (None: never synced). Values must be canonical: equal values
    compare equal."""
    base = base or {}
    names = list(fields) if fields is not None else sorted(set(local) | set(remote))
    result = MergeResult()
    for name in names:
        local_value, remote_value = local.get(name), remote.get(name)
        if local_value == remote_value:
            winner = None
        elif name not in base:
            if empty(remote_value):
                winner = LOCAL
            elif empty(local_value):
                winner = REMOTE
            else:
                winner = later(local_at, remote_at)
                result.conflicts.append(Conflict(name, local_value, remote_value, winner))
        elif local_value == base[name]:
            winner = REMOTE
        elif remote_value == base[name]:
            winner = LOCAL
        else:
            winner = later(local_at, remote_at)
            result.conflicts.append(Conflict(name, local_value, remote_value, winner))

        if winner == LOCAL:
            result.to_remote[name] = local_value
            result.merged[name] = local_value
        elif winner == REMOTE:
            result.to_local[name] = remote_value
            result.merged[name] = remote_value
        else:
            result.merged[name] = local_value
    return result


__all__ = ["LOCAL", "REMOTE", "Conflict", "MergeResult", "empty", "later", "merge"]
