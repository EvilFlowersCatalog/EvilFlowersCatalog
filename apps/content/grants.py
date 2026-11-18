"""Content grants (#175): what a reader holds or once held of a publication, as facts.

A row per Readium LCP licence and per own copy (`UserAcquisition`), written by signals on every save and kept
after its source ends. The gate (`apps.content.access`) reads these rows, and the policy's `grants.*` keys decide
which of them count:

- a licence in `grants.license_states`: `ready`/`active` is a loan, `returned`/`expired`/`revoked` a past loan;
- `expired` also counts as a past loan with `grants.expired_if_opened` when the reader opened it
  (`first_opened_at`): a `ready` loan can lapse unread;
- `cancelled` never counts: the Status Server cancels a loan returned before any reading app opened it;
- an own copy of a type in `grants.acquisition_types` is a loan of an entry without LCP.

Because rows are facts and not verdicts, a policy change applies at once without a backfill.
"""

import logging
from typing import Dict

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Licence states the reader can be reading in right now.
LIVE_LICENSE_STATES = ("ready", "active")
#: A licence state that means the reader opened it (a device registered or the loan was returned after use).
OPENED_LICENSE_STATES = ("active", "returned")


def _opened(license) -> bool:
    return license.state in OPENED_LICENSE_STATES or (license.device_count or 0) > 0


def record_license(license, *, opened_at=None) -> None:
    """Write or refresh the grant of one licence (idempotent). `opened_at` is when it was first opened, if new."""
    from apps.content.models import ContentGrant

    grant = ContentGrant.objects.filter(source=ContentGrant.Source.LICENSE, source_id=license.pk).first()
    first_opened_at = grant.first_opened_at if grant is not None else None
    if first_opened_at is None and _opened(license):
        first_opened_at = opened_at or timezone.now()
    values = {
        "user_id": license.user_id,
        "entry_id": license.entry_id,
        "source_state": license.state,
        "first_opened_at": first_opened_at,
        "granted_at": license.starts_at or license.created_at or timezone.now(),
        "revoked_at": None,
    }
    if grant is None:
        ContentGrant.objects.create(source=ContentGrant.Source.LICENSE, source_id=license.pk, **values)
        return
    if any(getattr(grant, name) != value for name, value in values.items()):
        ContentGrant.objects.filter(pk=grant.pk).update(**values, updated_at=timezone.now())


def record_acquisition(user_acquisition) -> None:
    """Write or refresh the grant of one own copy (idempotent)."""
    from apps.content.models import ContentGrant

    entry_id = getattr(user_acquisition, "_grant_entry_id", None)
    if entry_id is None:
        from apps.core.models import Acquisition

        entry_id = Acquisition.objects.filter(pk=user_acquisition.acquisition_id).values_list("entry_id", flat=True)
        entry_id = entry_id.first()
    if entry_id is None:
        return
    ContentGrant.objects.update_or_create(
        source=ContentGrant.Source.ACQUISITION,
        source_id=user_acquisition.pk,
        defaults={
            "user_id": user_acquisition.user_id,
            "entry_id": entry_id,
            "source_state": user_acquisition.type,
            "pages": user_acquisition.range,
            "granted_at": user_acquisition.created_at or timezone.now(),
            "revoked_at": None,
            "expires_at": user_acquisition.expire_at,
        },
    )


def revoke_acquisition(user_acquisition) -> None:
    """The copy was deleted: its grant stays as a fact but no longer counts."""
    from apps.content.models import ContentGrant

    now = timezone.now()
    ContentGrant.objects.filter(
        source=ContentGrant.Source.ACQUISITION, source_id=user_acquisition.pk, revoked_at__isnull=True
    ).update(revoked_at=now, updated_at=now)


def backfill() -> Dict[str, int]:
    """Grants for every existing licence and own copy (idempotent; for the first deploy of #175).

    A licence's `first_opened_at` is unknown for old rows: an opened one (`active`, `returned`, a device
    registered) gets its `starts_at`.
    """
    from apps.core.models import UserAcquisition
    from apps.readium.models import License

    counts = {"licenses": 0, "acquisitions": 0}
    with transaction.atomic():
        for license in License.objects.all().iterator(chunk_size=500):
            record_license(license, opened_at=license.starts_at)
            counts["licenses"] += 1
        rows = UserAcquisition.objects.select_related("acquisition").only(
            "id", "user_id", "type", "range", "created_at", "expire_at", "acquisition__entry_id"
        )
        for user_acquisition in rows.iterator(chunk_size=500):
            user_acquisition._grant_entry_id = user_acquisition.acquisition.entry_id
            record_acquisition(user_acquisition)
            counts["acquisitions"] += 1
    logger.info("content_grant.backfill licenses=%s acquisitions=%s", counts["licenses"], counts["acquisitions"])
    return counts


__all__ = ["LIVE_LICENSE_STATES", "backfill", "record_acquisition", "record_license", "revoke_acquisition"]
