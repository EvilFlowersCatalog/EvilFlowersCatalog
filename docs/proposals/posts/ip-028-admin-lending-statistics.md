---
draft: true
date: 2026-11-11
authors:
  - jdubec
categories:
  - Feature
tags:
  - api
  - readium
  - statistics
---

# IP-028: Admin Lending Statistics Endpoint

<!-- more -->

## Status

**Draft.** Implemented on the working tree (uncommitted, `develop`) against the contract in GitHub #131, with the
recommended answers below, and tested locally. Not yet checked against dev with the portal's dashboard. Awaiting
review answers.

## Problem Statement

The portal's admin home (`elvira-portal`, `src/pages/admin/AdminHome.tsx`) is getting an operational dashboard. It
answers what blocks lending now, where demand exceeds supply, how holds and loans end, and how lending moves over
time. None of it can be computed without paging every License and Reservation into the browser. #131 specifies
the endpoint and its contract field by field. This proposal records how it was built and which decisions the
issue left open.

## Proposed Solution

`GET /api/v1/statistics?catalog_id=&from=&to=&bucket=&tz=` returns `{"response": IAdminStatistics}` as defined in
#131 (`elvira-portal/src/utils/interfaces/statistics.ts`).

- **Access:** superuser, or `UserCatalog.Mode.MANAGE` on the catalog. Anonymous 401, others 403, unknown catalog
  404. Checks run before the cache lookup.
- **Validation:** 400 problem+json (`/validation-error`) for a bad UUID or date, `from > to`, more than 400 days,
  more than 400 buckets, an unknown `bucket` or an invalid IANA `tz`. `bucket` defaults to `day`, `tz` to
  `settings.TIME_ZONE`.
- **Caching:** the serialized response for 60 s, keyed by catalog, from, to, bucket, tz and the active language
  (category titles are localized).
- **Aggregates only:** no user ids, names or per-reader rankings leave the service.

## Implementation Plan

- [x] `apps/readium/services/statistics.py`: `build_statistics` and the response models.
- [x] `apps/readium/views/statistics.py`: validation, access, cache; route `statistics` in `apps/api/urls.py`.
- [x] Migration `readium/0010_statistics_indexes`: `License(entry, created_at)`, `License(created_at)`,
      `Reservation(requested_at)`. `Reservation(entry, status)` is already covered by the existing
      `(entry, status, position)` index.
- [x] `apps/readium/tests/test_statistics.py`: the test list from #131.
- [ ] Check the dashboard against dev without the **Demo data** badge, and one title by hand.
- [ ] Measure the 12-month response time on the production catalog (target < 300 ms on a cache miss).

## Technical Details

### Query plan

Six queries, independent of the number of titles and loans (asserted with `assertNumQueries(6)`), plus one or
two for the access check in the view:

1. LCP titles with copies, live licenses now, last loan ever and whether a `registered` package exists
   (correlated subqueries, so licenses and reservations never fan out against each other).
2. Authors of the titles returned (after the 1,000 cap).
3. Every license of the catalog created in, or overlapping, the period or the previous period.
4. Every reservation requested, promoted or resolved in those windows, plus all `queued`/`available` ones.
5. Encryption jobs `failed` or stuck (`pending`, last dispatch or creation older than 1 h).
6. Top 10 categories by licenses created in the period.

Licenses and reservations are folded in Python in one pass. Utilization and active readers are interval overlaps
with each window, which `Trunc` grouping cannot produce, and the same pass serves the period, the previous
period, every bucket and every title.

### Definitions

- **LCP title:** `config.readium_enabled` true and `readium_amount >= 1`, with `read_config` defaults (a missing
  amount is 1).
- **Loan interval:** `[starts_at, end)`. `end = min(expires_at, updated_at)` for `returned`, `revoked` and
  `cancelled`, otherwise `expires_at`. Returning moves `expires_at` to the return time only via the Status
  Server's document or the optimistic fallback in `StatusServerSync.return_license`; revoke and cancel never
  move it. The minimum is right in every case.
- **Copies in use:** live (`ready`/`active`, `expires_at > now`) licenses plus `available` reservations, as in
  `ReservationService._slots_in_use`.
- **Utilization:** loan-seconds of LCP titles inside the window ÷ (current total copies × window seconds). Per
  bucket the window is clipped to the period, so a partial bucket is a ratio over its covered part.
- **Hold fulfillment:** reservations whose terminal status was set in the window (by `updated_at`): claimed ÷
  (claimed + expired + cancelled).
- **Median wait:** median of `available_at − requested_at` in hours, for reservations with `available_at` in the
  window.
- **Average loan length:** licenses of the title whose end falls in the period and is already past.
- **Buckets:** calendar-aligned local dates (ISO week from Monday, first of month); `partial` when the bucket
  starts before `from` or ends after `to`.

## Alternatives Considered

- **SQL per bucket** (`generate_series` joined to licenses with `GREATEST`/`LEAST`): pushes the overlap work into
  Postgres but needs several raw queries and duplicates the loan-end rule in SQL. Worth it only if the Python
  fold measures too slow on production volumes.
- **Daily rollup table:** constant cost per request, but adds a writer, a backfill and staleness. Not needed at
  the current library size (about 1,000 titles).

## Trade-offs and Risks

- The fold reads every license overlapping the two windows into memory. At STU volumes this is thousands of rows
  per year; a much larger catalog would need the SQL variant.
- Utilization uses the current number of copies for past windows. Changing `readium_amount` rewrites history.
- Resolution time of a reservation is `updated_at`, which any later save of the row would move.

## Success Criteria

- The portal dashboard renders against dev without the **Demo data** badge.
- The numbers for one title match a hand computation.
- A 12-month request on the production catalog answers in under 300 ms on a cache miss.

## Future Considerations

- Download, shelf and review trends need an append-only event table or daily rollups in `ActivityService.record`
  (`UserActivity` is an upserted counter). Out of scope per #131.
- Cost per loan needs purchase prices per copy.

## References

- GitHub #131 (contract and field definitions)
- OverDrive Marketplace holds ratio; ISO 11620 / IFLA collection turnover and stock-not-used indicators

## Review Questions

**Status**: ⏳ Awaiting Answers
**Review Date**: 2026-11-11
**Reviewer**: Claude AI

The following questions must be answered before the implementation is considered final:

---

### Q1: ⚠️ Medium — Copies for past windows

**Issue**: Utilization, holds ratio and turnover divide by today's `readium_amount`, including for the previous
period and old buckets.

**Context**: Raising copies after a busy month makes that month look less utilized in hindsight.

**Question**: Is the current number of copies acceptable for past windows?

**Options**:
- [ ] **A**: Accept it; document it in the portal tooltip. (recommended — no copy history exists today)
- [ ] **B**: Record copy changes (history table) and use the count valid at each moment.

**Answer**:
```
```

**Resolution**:
```
```

---

### Q2: ⚠️ Medium — Reservation resolution time

**Issue**: Hold fulfillment buckets resolved reservations by `updated_at`, as #131 allows ("unless a dedicated
timestamp exists"). None exists.

**Context**: Any later save of a terminal reservation would move it into a different bucket.

**Question**: Add a `resolved_at` column set on the terminal transition?

**Options**:
- [ ] **A**: Keep `updated_at` for now. (recommended — terminal rows are not saved again today)
- [ ] **B**: Add `Reservation.resolved_at`, backfilled from `updated_at`.

**Answer**:
```
```

**Resolution**:
```
```

---

### Q3: ℹ️ Low — Which licenses count as active readers

**Issue**: Active readers counts every license whose interval overlaps the period, including `cancelled` ones
that were never opened (their interval ends at cancellation).

**Question**: Should licenses cancelled while still `ready` be excluded?

**Options**:
- [ ] **A**: Count them; the reader did hold a copy. (recommended)
- [ ] **B**: Exclude `cancelled`.

**Answer**:
```
```

**Resolution**:
```
```

---

### Q4: ℹ️ Low — Claims past their deadline

**Issue**: `claims_expiring_24h` counts `available` reservations with `claim_deadline < now + 24h`, which also
includes deadlines already passed but not yet swept.

**Question**: Keep the overdue ones in this tile?

**Options**:
- [ ] **A**: Keep them; they need attention too. (recommended)
- [ ] **B**: Only `now <= claim_deadline < now + 24h`.

**Answer**:
```
```

**Resolution**:
```
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-11-11 | jdubec | Initial draft from GitHub #131, implemented with recommended answers and tested locally |
| 2026-11-11 | jdubec | Added Review Questions section |
