# Predicate Audit — IP-004 Phase 4 (Q2 Resolution)

**Date:** 2026-05-14
**Reviewer:** Claude AI, jdubec
**Scope:** `apps/core/checkers.py`

This audit was produced as part of IP-004 Phase 4 to verify that the conflated-predicate antipattern fixed in `LicenseChecker` does not recur elsewhere in the permission layer. Each predicate is classified as:

- **SPLIT** — fix in this PR (different operations with materially different blast radii share a single predicate).
- **INTENTIONAL** — conflation is correct; document the rationale.
- **DEFERRED** — worth revisiting later; open a follow-up issue.

## Findings

### `LicenseChecker` — **SPLIT** (done in IP-004 Phase 4)

**Before:** `check_license_manage(user, obj) -> obj.user == user` gated both:

- `PATCH /readium/v1/licenses/{id}` (state transitions: return / revoke / cancel — admin-appropriate operations).
- `GET /readium/v1/licenses/{id}.lcpl` (content download — user-private artifact).

The same predicate covering both surfaces meant that any widening to admit catalog managers would also grant them download access — an exfiltration vector EDRLab would flag.

**After:** Split into two predicates with independent blast radii:

| Predicate | Admits | Gates |
|-----------|--------|-------|
| `check_license_state_manage` | owner OR catalog MANAGE OR superuser | `PATCH /readium/v1/licenses/{id}` |
| `check_license_download` | owner only | `GET /readium/v1/licenses/{id}.lcpl`, License Gateway content fetch |

Break-glass content access (e.g., for incident response) is intentionally **not** an API surface — it must go through an audited management command.

Call sites updated:

- `apps/readium/views/licenses.py:120` → `check_license_state_manage`
- `apps/readium/views/download.py:35` → `check_license_download`

### `UserAcquisitionChecker.check_user_acquisition_read` — **INTENTIONAL**

```python
def check_user_acquisition_read(user, obj):
    if obj.type == UserAcquisition.UserAcquisitionType.SHARED:
        return True
    return obj.user == user
```

Used in:

- `apps/files/views.py:130` — encrypted-content download for `PERSONAL` acquisitions (already inside an outer guard that bypasses this check for `SHARED`).
- `apps/api/views/user_acquisitions.py:107` — metadata read.
- `apps/api/views/annotations.py:40,70` and `apps/api/views/annotation_items.py:36,67` — annotation listing/read.

**Rationale:** `SHARED` is the auth mechanism itself, not a relaxation of one. The product contract is "a user can opt to share their acquisition publicly." Conflation of metadata-read and content-download under a single predicate is correct because both axes derive from the same policy decision (shared-or-owner).

**Note:** The download path in `files/views.py:129` already short-circuits for `SHARED` before consulting the predicate, which makes this an INTENTIONAL but slightly redundant call. Cosmetic cleanup; not a security concern.

### `EntryChecker.check_entry_manage` — **DEFERRED**

```python
def check_entry_manage(user, obj):
    if obj.creator_id == user.id:
        return True
    return obj.catalog.user_catalogs.filter(user=user, mode=UserCatalog.Mode.MANAGE).exists()
```

Used in `apps/api/views/entries.py:171` as the default checker for:

- PUT (entry edit, including `config.readium_amount` — the headline IP-004 change).
- POST (add acquisition).
- DELETE (entry removal — including cascading delete of acquisitions, annotations, shelf records).

**Question:** should deletion require a strictly stronger predicate than edit?

**Decision:** **DEFERRED.** The current `UserCatalog.Mode.MANAGE` role is already the highest-trust catalog-scoped role; granting it includes the implicit decision to allow destructive operations. A more granular role model (e.g., separating `EDIT` from `DELETE`) would be a product change, not a security fix. Open a follow-up issue if/when the role model evolves: <https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/new?title=Granular+entry+role+model>.

### `CatalogChecker.check_catalog_read` — **INTENTIONAL**

```python
def check_catalog_read(user, obj):
    if user.is_superuser or obj.is_public:
        return True
    if not user.is_authenticated:
        return False
    return obj.users.contains(user)
```

Used by OPDS feed views (`apps/opds/views/base.py:34`, `apps/opds2/views/base.py:38`) and by `apps/api/views/catalogs.py`, `authors.py`, `acquisitions.py`, `categories.py`, `feeds.py` as the default checker for nested-resource reads.

**Rationale:** All call sites return catalog **metadata** (entry listings, author lists, category trees, feeds) — none return protected content. The predicate is single-axis (catalog membership + public flag); there's no second operation type to split off.

### `ShelfRecordChecker.check_shelf_record_access` — **INTENTIONAL**

```python
def check_shelf_record_access(user, obj):
    return obj.user == user
```

Narrowly scoped, owner-only, personal data. Single-axis by design.

### `CatalogChecker.check_catalog_manage` and `check_catalog_write` — **INTENTIONAL**

`MANAGE` is the catalog-administration role and is checked exclusively at write endpoints; `WRITE` is checked at content-write endpoints. The predicates are not used as content-gating tools and have no conflation issue.

## Summary

| Predicate | Classification | Action |
|-----------|----------------|--------|
| `LicenseChecker.check_license_manage` | **SPLIT** | Done in IP-004 Phase 4. |
| `UserAcquisitionChecker.check_user_acquisition_read` | INTENTIONAL | No change. |
| `EntryChecker.check_entry_manage` | DEFERRED | Revisit if role model gains granularity. |
| `CatalogChecker.check_catalog_read` | INTENTIONAL | No change. |
| `ShelfRecordChecker.check_shelf_record_access` | INTENTIONAL | No change. |
| `CatalogChecker.check_catalog_manage`, `check_catalog_write` | INTENTIONAL | No change. |

## Follow-up Issues

None opened from this audit. The DEFERRED finding on `check_entry_manage` is a product-direction question, not a security concern.

## How to Re-run This Audit

Whenever a new checker predicate is added to `apps/core/checkers.py`, repeat the classification exercise:

1. Identify every call site (`grep -rn "check_<name>" apps/`).
2. For each call site, categorize the operation: **metadata read**, **content download**, **state change**, **deletion**.
3. If a single predicate gates more than one of those categories with materially different blast radii, **SPLIT** it.
