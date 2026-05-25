---
draft: true
date: 2026-05-14
authors:
  - jdubec
categories:
  - Architecture
tags:
  - multi-tenancy
  - acl
  - permissions
  - security
---

# IP-010: Multi-Tenancy & ACL Correctness

The catalog's headline access-control pattern — every queryset filters through `BaseSecuredFilter.apply_catalog_access_control` and every write checks `has_object_permission("check_*")` — is correct on the major surfaces. But the audit found a peripheral cluster where the pattern is bypassed: cover/thumbnail file downloads, shelf-record creation, cross-tenant category uniqueness, the catalog-ACL cache (5-minute TTL, no invalidation on `UserCatalog` writes), and several management commands. None of these are critical alone, but together they mean a multi-tenant deployment (STU plus future Slovak universities) leaks data and serves stale permission decisions for up to 5 minutes after an admin change. This proposal closes them as one bundled change.

<!-- more -->

## Status

**Status**: Draft
**Last Updated**: 2026-05-14
**Implementation**: Not started

## Problem Statement

### M1. `UserCatalog` ACL cache is 5 minutes TTL with no invalidation

- `apps/api/filters/base.py:38-54`. `_get_user_catalog_access` caches per-user catalog access decisions for 5 minutes.
- `apps/core/auth.py:273` (LDAP group reconciliation creates `UserCatalog` rows) and `apps/api/services/catalog.py:11-13` (catalog admin updates users) both insert/delete `UserCatalog` rows without flushing the cache.
- A freshly-granted access doesn't take effect for up to 5 minutes; a freshly-revoked access keeps working for up to 5 minutes.

### M2. Cover/thumbnail downloads bypass catalog ACL

- `apps/files/views.py:193-222`. `EntryImageDownload.get` and `EntryThumbnailDownload.get` only filter by `image__isnull=False`.
- Anyone with a UUID downloads covers from private catalogs.

### M3. Shelf-record creation bypasses entry-read check

- `apps/api/views/shelf_records.py:55-57`. `ShelfRecord.objects.get_or_create(user=request.user, entry=…)` doesn't call `check_entry_read`.
- A user can confirm existence of (or add to their shelf) any entry by UUID, including from catalogs they cannot read. The shelf is then visible on their personal shelf feed.

### M4. Cross-tenant `Category` uniqueness leak

- Already covered in IP-007 D17. `apps/api/views/categories.py:33, 104`.
- The view-level check spans all tenants; the DB constraint is correctly scoped.

### M5. `load_catalog` accepts arbitrary PKs and overwrites cross-tenant

- `apps/core/management/commands/load_catalog.py:47-53`. Deserialized objects are saved without checking PK collisions across catalogs. The command rewrites `creator_id` to the first superuser regardless of tar contents (line 52).
- An attacker with `--input` access can overwrite arbitrary core objects in any tenant.

### M6. `FeedManagement.post` and `FeedDetail.delete` use read-only permission

- Already covered in IP-007 D18. Read-only users can create / delete feeds.

### M7. `Feed` filter excludes public catalogs from non-superuser results

- `apps/api/filters/feeds.py:62-68`. The non-superuser queryset filters strictly by `UserCatalog`, never including `Catalog.is_public=True`.
- Compare with `apps/api/filters/categories.py:57` which does include public catalogs. Inconsistent.

### M8. `popularity` increment is a race-condition read-modify-write

- `apps/files/views.py:142-143`. `acquisition.entry.popularity = acquisition.entry.popularity + 1; entry.save()`.
- Concurrent downloads lose counts. Should be `Entry.objects.filter(pk=...).update(popularity=F("popularity")+1)`.
- The OPEN_ACCESS path (`apps/files/views.py:113-119`) and the `format=base64` short-circuit (line 116) also drop the increment entirely.

### M9. Asymmetric author/acquisition sync from Dataverse

- Covered in IP-009 Cluster E. `apps/api/views/dataverse.py:749` — author clearing only when the new list is truthy; acquisitions never removed when upstream removes a file.

### M10. `purge_catalog` and `dump_catalog` no S3 support

- `apps/core/management/commands/dump_catalog.py:137-141` — only writes storage files when filesystem driver is configured. S3 backups silently exclude all media.
- `apps/core/management/commands/purge_catalog.py:47-53` — S3 path is a warning + no-op; orphan files accumulate.

### M11. Admin has no per-tenant scoping

- `apps/core/admin.py` has no `get_queryset` override that filters by `UserCatalog`. Currently OK because only superusers access admin. If non-superusers are ever granted `is_staff`, every model row is visible.

### M12. `UserAcquisition` has no DELETE endpoint

- `apps/api/views/user_acquisitions.py` exposes create + list but no delete. The data model expects revocation; the API doesn't allow it.

### Who is Affected

- **STU library staff revoking a user's catalog access** — the revocation doesn't take effect for 5 minutes (M1).
- **Library patrons** — covers from private catalogs are leak-able (M2); their shelf can be polluted by guessing UUIDs (M3).
- **Operators of multi-tenant deployments** — categories block each other across tenants at the view layer (M4); feed filters miss public catalogs (M7).
- **SREs** — backups exclude S3 storage silently (M10).
- **Future Slovak universities** — they inherit every multi-tenancy gap.

### Consequences of Not Addressing

- A multi-tenant rollout has visible cross-tenant leakage at the periphery.
- Cache staleness undermines the IP-004 admin workflow (revoke a user → expect them to lose access immediately → they don't).
- S3-only deployments have no usable backup or purge command.

## Proposed Solution

### Overview

Three phases. Phase 1 (cache invalidation + ACL fixes) ships as a single PR. Phase 2 (filter/queryset consolidation) cleans up the asymmetry. Phase 3 (management commands + admin) is the low-priority cleanup.

### Key Components

1. **Cache invalidation on `UserCatalog` writes** — flush the per-user cache from a `post_save`/`post_delete` signal on `UserCatalog`.
2. **ACL checks added** — covers, thumbnails, shelf-record creation; permission scope fix for feeds + categories (overlaps with IP-007 D17, D18 — coordinate in one PR).
3. **`Feed` filter consistency** — include `Catalog.is_public` rows in non-superuser results, matching `CategoryFilter`.
4. **`F()` increment for popularity** — atomic update; populate on all download paths.
5. **`load_catalog` tenancy** — refuse to load objects whose PK already exists in another catalog; require explicit `--allow-overwrite`.
6. **S3-aware management commands** — `dump_catalog` and `purge_catalog` handle both filesystem and S3 storage drivers.
7. **Admin scoping** (deferred to when non-superuser admin is needed; for now, just document).
8. **UserAcquisition DELETE** — round out the CRUD.

### Architecture

```mermaid
flowchart LR
    subgraph WRITE[UserCatalog write]
        ADMIN[Catalog admin] -->|PUT /catalogs/{id}/users| SVC[CatalogService.populate]
        SVC -->|delete + create| UC[(UserCatalog rows)]
        UC -->|post_save / post_delete| INV[Cache.delete<br/>per-user key]
    end
    subgraph READ[Any subsequent request]
        REQ[Authenticated request] --> FILT[BaseSecuredFilter<br/>apply_catalog_access_control]
        FILT -->|cache miss| DB[(UserCatalog query)]
        DB --> CACHE[(Cache 5-min)]
        FILT -->|cache hit| CACHE
    end
    INV -.->|invalidate| CACHE
    style INV fill:#cfc,stroke:#393
```

## Implementation Plan

### Phase 1: ACL Cache Invalidation + Missing Permission Checks

- [ ] **M1** — Add `apps/core/signals.py::on_user_catalog_change(sender=UserCatalog, **kwargs)` connecting `post_save` and `post_delete`. Body: `cache.delete(f"user_catalog_access:{instance.user_id}")` matching the key in `apps/api/filters/base.py:38-54`.
- [ ] **M1** — Add tests: assert that `cache.get(f"user_catalog_access:{u.id}")` returns None immediately after `UserCatalog.objects.create(user=u, catalog=c)` and after `delete()`.
- [ ] **M2** — `apps/files/views.py:193-222` — add `has_object_permission("check_catalog_read", request.user, entry.catalog)` before serving the cover/thumbnail. Short-circuit on `entry.catalog.is_public=True` if public-catalog images are intentional (Q1). Tests: anonymous request against a private-catalog cover returns 403; against a public-catalog cover returns 200.
- [ ] **M3** — `apps/api/views/shelf_records.py:46-57` — add `entry = self._get_entry(entry_id, check="check_entry_read", user=request.user)` before `get_or_create`. Tests: read-only on the entry's catalog allows shelf-add; no-access returns 403.
- [ ] **M4** — Ships in IP-007 D17.
- [ ] **M6** — Ships in IP-007 D18.
- [ ] **M8** — `apps/files/views.py:142-143` — change to `Entry.objects.filter(pk=acquisition.entry_id).update(popularity=F("popularity")+1)`. Apply same `F()` pattern to all download paths (OPEN_ACCESS at line 113-119; format=base64 at line 116). Tests: 100 concurrent downloads result in 100 popularity increments.

### Phase 2: Filter / Queryset Consistency

- [ ] **M7** — `apps/api/filters/feeds.py:62-68` — replace the strict `UserCatalog` filter with the same pattern used in `apps/api/filters/categories.py:57`: `UserCatalog rows OR Catalog.is_public=True`. Tests: non-superuser sees feeds in catalogs they have access to AND in public catalogs.
- [ ] **General consolidation** — `apps/api/filters/base.py:77-93` `apply_related_catalog_access_control` and `apply_catalog_access_control` are near-duplicates differing in a string template. Extract one helper parameterized by the kwarg path.
- [ ] **General consolidation** — Document the canonical access-control predicate in `docs/security/multi-tenancy-acl.md`: for non-superusers, every list endpoint must filter `catalog__user_catalogs__user=request.user OR catalog__is_public=True`. Audit each filter against this rule and add tests for the asymmetric case (private vs public catalog).

### Phase 3: Management Commands + Admin

- [ ] **M5** — `apps/core/management/commands/load_catalog.py:47-53` — refuse to load objects whose PK already exists in *another* catalog. Require `--allow-overwrite` flag to acknowledge. Refuse to rewrite `creator_id` to the first superuser without a `--creator-id` flag.
- [ ] **M10** — `apps/core/management/commands/dump_catalog.py:137-141` — accept S3 storage driver: list S3 objects under the catalog's prefix and download them into the dump tarball.
- [ ] **M10** — `apps/core/management/commands/purge_catalog.py:47-53` — accept S3 storage driver: delete S3 objects under the catalog's prefix.
- [ ] **M11** — Add `apps/core/admin.py::BaseAdminMixin` with a default `get_queryset` that filters by `UserCatalog` when `not request.user.is_superuser`. No-op for current superuser-only access; ready for the day non-superuser admin is enabled.
- [ ] **M12** — Add `DELETE /api/v1/user-acquisitions/{id}` to `apps/api/views/user_acquisitions.py`. Permission: owner OR catalog manager. Tests covering both.

## Technical Details

### Data Model Changes

None. The fixes are at the view, filter, signal, and management-command layers.

### API Changes

- `GET /api/v1/files/entries/{id}/cover` and `.../thumbnail` — now return 403 for users without read access to the entry's catalog (previously 200).
- `POST /api/v1/shelf-records` — now returns 403 when the entry is in an inaccessible catalog (previously created the row).
- `DELETE /api/v1/user-acquisitions/{id}` — new endpoint.

### Configuration

```sh
EVILFLOWERS_CACHE_USER_CATALOG_TTL_SECONDS=300  # already implicit; expose
```

## Alternatives Considered

### Alternative 1: Drop the cache; query `UserCatalog` on every request

**Pros**: No cache-invalidation logic.

**Cons**: `UserCatalog` is checked on every list endpoint; the cache halves DB load. Worth keeping.

**Why not chosen**: Invalidate, don't drop.

### Alternative 2: Use a TTL of 60 seconds instead of 5 minutes

**Pros**: Smaller staleness window without explicit invalidation.

**Cons**: Still wrong; just less wrong. Doesn't help the operator-told-the-system-to-revoke-now case.

**Why not chosen**: Invalidate.

### Alternative 3: Public catalog covers/thumbnails return 200 regardless of auth

**Pros**: Simpler view code; CDN-friendly.

**Cons**: A catalog admin could mark a catalog private and the covers stay accessible because of CDN caching. Need cache-busting on visibility change.

**Why not chosen**: Recommended path — Q1 resolves whether public-catalog covers are accessible to anonymous users.

## Trade-offs and Risks

### Trade-offs

- **Cache invalidation adds a DB write hook on every `UserCatalog` change.** Negligible.
- **Cover/thumbnail ACL adds a DB roundtrip per image download.** Acceptable; image downloads are not high-frequency.
- **`F()` popularity update means we no longer log "before" and "after" values from the ORM.** Acceptable trade-off.

### Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Cache invalidation signal not wired into the LDAP `_ldap` code path (`apps/core/auth.py:273`) because it bulk-creates without firing signals | High | Verify signal fires; if `bulk_create` is used, switch to a loop of `create` or `.delete()`+`.create()` to ensure signals fire. Add an integration test. |
| Cover ACL breaks the SPA's expectation that covers are public | Low | Q1 picks the policy; document. |
| Shelf-record ACL breaks an SPA workflow that pre-adds entries before fetching them | Low | Likely no such workflow; verify with the SPA team. |
| `F()` increment skips post_save signals | Low | `popularity` doesn't drive any signal today; verify. |
| `load_catalog` tenancy rejection breaks an existing operator workflow | Medium | `--allow-overwrite` opt-in preserves the old behavior for the rare legitimate case. |

## Success Criteria

- [ ] Revoke a user's catalog access via `DELETE /api/v1/catalogs/{id}/users/{u_id}` → the next request from that user is 403 (not "still works for 5 minutes").
- [ ] `GET /api/v1/files/entries/{id}/cover` returns 403 for an anonymous request against a non-public catalog.
- [ ] `POST /api/v1/shelf-records` with an entry from an inaccessible catalog returns 403.
- [ ] Two `Category` rows with the same `term` in two different catalogs both create successfully (IP-007 D17 prerequisite).
- [ ] Read-only-permission user receives 403 on `POST /api/v1/feeds/...` and `DELETE /api/v1/feeds/{id}` (IP-007 D18).
- [ ] A non-superuser sees feeds in public catalogs they don't otherwise have access to.
- [ ] 100 concurrent downloads of the same Entry produce `popularity += 100`, not less.
- [ ] `python manage.py load_catalog --input dump.tgz` refuses to overwrite PKs that exist in a different catalog (without `--allow-overwrite`).
- [ ] `python manage.py dump_catalog --name X` with `EVILFLOWERS_STORAGE_DRIVER=S3Storage` includes media files in the dump tarball.
- [ ] `DELETE /api/v1/user-acquisitions/{id}` succeeds for the owner and for catalog managers; 403 otherwise.

## Future Considerations

- A dashboard query showing per-user effective catalog access (useful for support tickets).
- Object-level permissions API: a `GET /api/v1/me/permissions?catalog={id}` endpoint returning a list of allowed actions.
- Soft delete on `UserCatalog` to retain audit history.
- Per-tenant rate limits (the current rate-limiting story is undocumented).

## References

- `apps/api/filters/base.py:38-54` — ACL cache (M1).
- `apps/files/views.py:193-222` — cover/thumbnail (M2).
- `apps/api/views/shelf_records.py:55-57` — shelf-record (M3).
- `apps/api/filters/feeds.py:62-68` — feed filter (M7).
- `apps/files/views.py:142-143` — popularity race (M8).
- `apps/core/management/commands/load_catalog.py:47-53` — tenant overwrite (M5).
- [IP-004: Per-Entry Active-License Limits](ip-004-readium-amount-configurability.md) — IP-004's admin workflow is undermined by M1.
- [IP-007: Crash-on-First-Contact Bug Triage](ip-007-crash-on-first-contact-triage.md) — D17 (category cross-tenant) and D18 (feed permission) coordinated.
- [IP-008: Readium + Dataverse Post-Merge Consolidation](ip-008-readium-correctness-followup.md) — Phase 5 (`AcquisitionStorageService`) shares the auth-before-redirect fix at the storage-service dispatch point that this IP's M2 region depends on.

## Review Questions

**Status**: ⏳ Awaiting Answers
**Review Date**: 2026-05-14
**Reviewer**: Claude AI

---

### Q1 ⚠️: Cover/thumbnail visibility — auth-required or public-catalog-OK?

**Issue**: M2 adds an ACL check to cover/thumbnail downloads. Two reasonable policies: (a) anonymous can fetch covers from `Catalog.is_public=True`, (b) all cover access requires auth (matching entry detail).

**Question**: Match catalog visibility, or always require auth?

**Options**:

- [ ] **A**: Match `Catalog.is_public` — anonymous can fetch covers from public catalogs (Recommended). Matches the OPDS-1.2 anonymous-feed contract.
- [ ] **B**: Always require auth.
- [ ] **C**: Per-entry visibility flag (`Entry.cover_is_public`). Overkill.

**Answer**:
```
[User fills this in]
```

**Resolution**:
```
[AI writes this — describes how proposal will be updated based on the user's answer]
```

---

### Q2 ⚠️: M11 admin scoping — implement now or defer?

**Issue**: Today only superusers access Django admin; per-tenant scoping is unnecessary. Implementing it preemptively adds a `get_queryset` override on every ModelAdmin and a small risk of "I changed something in admin and now it's filtered."

**Question**: Implement defensively, or document and defer?

**Options**:

- [ ] **A**: Document and defer (Recommended). Add a comment in `apps/core/admin.py` noting the gap; revisit when non-superuser admin is requested.
- [ ] **B**: Implement now via a mixin so future enablement is one-line per ModelAdmin.

**Answer**:
```
[User fills this in]
```

**Resolution**:
```
[AI writes this — describes how proposal will be updated based on the user's answer]
```

---

### Q3 ℹ️: M5 `load_catalog` tenancy — refuse cross-tenant, or remap?

**Issue**: When a tarball includes objects whose PK already exists in a different catalog, today the command silently overwrites. The audit recommends refusing without `--allow-overwrite`.

**Question**: Refuse or remap (re-issue PKs)?

**Options**:

- [ ] **A**: Refuse without `--allow-overwrite` (Recommended). Operator must explicitly opt into destructive load.
- [ ] **B**: Remap — re-issue PKs on import. Loses upstream IDs.
- [ ] **C**: Require the tarball to declare its target catalog in metadata; refuse mismatches.

**Answer**:
```
[User fills this in]
```

**Resolution**:
```
[AI writes this — describes how proposal will be updated based on the user's answer]
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-05-14 | Claude AI | Initial draft based on May 2026 core/api/files/tasks audit. |
| 2026-05-25 | jdubec | Dropped cross-reference to former IP-005 (security hardening). Added cross-reference to IP-008 (consolidation) for the shared `AcquisitionStorageService` dispatch point. |
