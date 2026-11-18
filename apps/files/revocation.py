"""
Revocation tokens for the database-free range path (IP-036 Phase 3).

A range served from a pin (`personalised.pin`) skips the database, so it cannot see that its grant was
revoked, its reader deactivated or its entry's IP block switched on. The pin records a **token** per subject
(the grant, the user who reads, the entry); a range is served from the pin only while all three tokens are
still the same.

- A token is a random 62-bit number in the cache (`revocation:<kind>:<id>`), never a counter. Revoking drops it;
  the next full path mints a new random value, so any pin made before no longer matches.
- A cache error while revoking is logged, never raised (`on_commit(robust=True)`): a Redis outage must not break
  logins or entry edits. The cost is a revocation late by at most the pin's maximum age.
- A missing token means "unknown" (IP-036 Q4): the range takes the full path, which mints a new token. An evicted
  or flushed token therefore costs speed, never safety; it can never come back as the value an old pin holds.
- Order (no race): the full path reads (or mints) the tokens **before** it loads the grant, and pins with those
  tokens. A revocation that commits in between changes the token, so the pin it writes is stale at once.
- Revocations run after commit (`transaction.on_commit`): a token changed before the commit could be paired by
  another request with the old row.
- Every save or delete of a grant, user or entry revokes (no field diffing). Users are saved at login and on
  account changes, entries on edits; reads save neither (#214), so a revocation costs at most one full-path
  range.

Tokens expire after `TOKEN_SECONDS` of not being minted or revoked again; a pin never outlives `PIN_MAX_SECONDS`
(`personalised`), so a token's expiry only ever sends a range to the full path.
"""

import logging
import secrets
from typing import Dict, Iterable, Optional, Tuple

from django.core.cache import cache
from django.db import transaction

logger = logging.getLogger(__name__)

#: Longer than any pin lives; a token gone after this only costs one full-path range.
TOKEN_SECONDS = 7 * 24 * 3600

GRANT = "ua"
USER = "user"
ENTRY = "entry"

Subject = Tuple[str, str]


def _key(kind: str, subject_id) -> str:
    return f"revocation:{kind}:{subject_id}"


def _fresh() -> int:
    return secrets.randbits(62) + 1


def tokens(subjects: Iterable[Subject]) -> Dict[Subject, Optional[int]]:
    """The current token of each `(kind, id)`, None where it is missing (one cache round trip)."""
    subjects = list(subjects)
    found = cache.get_many([_key(kind, subject_id) for kind, subject_id in subjects])
    return {(kind, subject_id): found.get(_key(kind, subject_id)) for kind, subject_id in subjects}


def mint(subjects: Iterable[Subject]) -> Dict[Subject, int]:
    """The current token of each subject, created where missing. Call before loading the rows you pin."""
    subjects = list(subjects)
    current = tokens(subjects)
    for subject, token in current.items():
        if token is None:
            cache.add(_key(*subject), _fresh(), TOKEN_SECONDS)
    return {subject: token for subject, token in tokens(subjects).items() if token is not None}


def _drop(key: str) -> None:
    try:
        cache.delete(key)
    except Exception:
        # Never fails the save that revoked: a token that could not be dropped stays valid until it expires or
        # the pin it guards ages out (`personalised.PIN_MAX_SECONDS`); the full path still checks every request.
        logger.exception("revocation.drop_failed key=%s", key)


def revoke(kind: str, subject_id) -> None:
    """Invalidate every pin of this subject, after the surrounding transaction commits.

    Dropping the token is enough: a missing token sends the range to the full path, which mints a new random one
    (never the old value). A cache error is logged and never breaks the save or the other on-commit hooks.
    """
    key = _key(kind, subject_id)
    transaction.on_commit(lambda: _drop(key), robust=True)


def matches(pinned: Dict[str, int], subjects: Dict[str, Subject]) -> bool:
    """Whether every token a pin recorded (`{name: token}`) is still current. Missing tokens never match."""
    current = tokens(subjects.values())
    for name, subject in subjects.items():
        token = current.get(subject)
        if token is None or pinned.get(name) != token:
            return False
    return True
