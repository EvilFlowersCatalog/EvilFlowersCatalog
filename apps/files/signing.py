"""
Reusable signed download URLs for one user acquisition (#214).

pdf.js reads a book in dozens of range requests and sends the same headers with
every one of them, so a bearer token that expires mid-book breaks the read, and
an `Authorization` header costs a CORS preflight per origin. A signed URL
carries its own credential:

    /data/v1/user-acquisitions/<id>?exp=<unix seconds>&sig=<hmac>

- `sig` = HMAC-SHA256 (keyed by `SECRET_KEY`, salted) over the user acquisition
  id and the expiry. It names one user acquisition and nothing else.
- Reusable until `exp`: pdf.js requests the same URL many times, so a
  single-use link would break it.
- It stands in for the owner's credential only. Every request still runs the
  IP block and the licence checks, and the owner must still be active.
"""

import time
import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Optional, Tuple

from django.conf import settings
from django.utils.crypto import constant_time_compare, salted_hmac

SALT = "evilflowers.files.user-acquisition-stream"


def _signature(user_acquisition_id, expires: int) -> str:
    return salted_hmac(SALT, f"{user_acquisition_id}:{expires}", algorithm="sha256").hexdigest()[:40]


#: `exp` is rounded down to this step, so the user acquisition's JSON is the same for a few minutes and a
#: revalidation can still answer 304. The URL stays valid TTL minus at most one step.
STEP_SECONDS = 300


def sign(user_acquisition, now: Optional[float] = None) -> Tuple[int, str]:
    """`(exp, sig)` for a URL valid ~`EVILFLOWERS_FILES_SIGNED_URL_TTL` seconds, never past the acquisition's expiry."""
    expires = int(now if now is not None else time.time()) + int(settings.EVILFLOWERS_FILES_SIGNED_URL_TTL)
    expires -= expires % STEP_SECONDS
    expire_at = getattr(user_acquisition, "expire_at", None)
    if expire_at is not None:
        expires = min(expires, int(expire_at.timestamp()))
    return expires, _signature(user_acquisition.pk, expires)


def signed_query(user_acquisition, now: Optional[float] = None) -> Tuple[str, datetime]:
    """The `exp=…&sig=…` query string and the moment it stops working."""
    expires, signature = sign(user_acquisition, now)
    return f"exp={expires}&sig={signature}", datetime.fromtimestamp(expires, tz=dt_timezone.utc)


def verify(user_acquisition_id: uuid.UUID, expires: Optional[str], signature: Optional[str]) -> bool:
    """True when `signature` was minted for this user acquisition and `expires` has not passed."""
    if not expires or not signature:
        return False
    try:
        expires_at = int(expires)
    except ValueError:
        return False
    if expires_at < time.time():
        return False
    return constant_time_compare(_signature(user_acquisition_id, expires_at), signature)
