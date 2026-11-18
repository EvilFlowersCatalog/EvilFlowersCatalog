---
draft: true
date: 2026-11-18
authors:
  - jdubec
categories:
  - Files
  - Performance
  - Security
tags:
  - user-acquisition
  - streaming
  - pdf
  - range-requests
  - watermark
  - caching
  - revocation
---

# IP-036: Reader Grants and a Database-Free Range Path — User Acquisitions That Stream Well

A user acquisition is used as an access grant, as the identity printed in the watermark, as the anchor of
annotations, and as the key of the personalised copy. #214 made PDF downloads range-capable, but this
mix decides how often a book is rendered and how much each range costs. This proposal separates the
grant from the copy and gives range requests a path that needs no database, while keeping revocation
immediate.

<!-- more -->

## Status

✅ **Accepted** (2026-11-18). Review Questions Q1–Q10 answered interactively; the body below follows the answers.
Phases 0–5 implemented and tested locally, uncommitted on `develop` (Phase 0 is #215, closed). Phase 6 (dev
measurement) open, knowledge#88. Builds on #214 (closed, `2c7cd8e` plus uncommitted follow-ups).

## Problem Statement

### One row, four jobs

`UserAcquisition` (`apps/core/models/user_acquisition.py`) carries `type` (personal, shared, LCP), `range`
(allowed pages) and `expire_at`. Today it is:

1. **An access grant.** `check_user_acquisition_read` admits the owner (or anyone, for `shared`). The portal's
   admin "access grants" screen lists them (`elvira-portal` `useAdminAccessGrants.tsx`).
2. **The watermark identity.** The licence page prints "Document ID: `<ua.id>`", and the QR on every page encodes
   the same context (`apps/files/views.py`, `templates/files/license.txt`).
3. **The annotation anchor.** `Annotation.user_acquisition` is a foreign key.
4. **The copy key.** `personalised.copy_name` hashes the user acquisition id together with the context that
   contains it.

`EVILFLOWERS_USER_ACQUISITION_MODE` sets all four at once:

| | `single` (default) | `multiple` |
|---|---|---|
| `POST /api/v1/user-acquisitions` | `303` to the user's existing personal row | a new row every time |
| Personalised copy | reused until idle (24 h) | **a new render on every open** |
| Annotations | one anchor per reader and acquisition | spread across one row per open |
| Watermark identifies | the reader's one grant | the individual open |

### Defects found on the way (2026-11-18)

- **`single` ignores `range`.** The reuse lookup matches user, acquisition and type only. A request for other
  pages gets the old row, with the old range (`apps/api/views/user_acquisitions.py:60–77`).
- **`expire_at` is never enforced** on any read path, and `single` reuse hands back expired rows (#215).
- **No `DELETE` handler.** The portal calls `DELETE /api/v1/user-acquisitions/<id>`, but `UserAcquisitionDetail`
  defines only `get` (to confirm: Review Questions).

### What a range costs after #214

pdf.js opens a 6.7 MB book in roughly 25–100 range requests, depending on `rangeChunkSize`. After #214, each
range request still does all of this:

- `SecuredView.dispatch`: JWT verification plus a user `SELECT`, or the HMAC of a signed URL plus a
  `UserAcquisition` `SELECT` with three joins;
- the rate-limit counter;
- `check_user_acquisition_read` and the IP block;
- a pin read and a marker touch in Redis (`personalised.pinned`);
- tracing spans;
- the storage read.

The browser-to-edge leg is HTTP/2 or HTTP/3, so the request count is cheap on the wire. Behind the edge, every
range is still a separate Django request into a gevent worker. KU had 2 workers on 2026-11-17.

## Proposed Solution

### A. Grants are reused when equivalent, not by mode

A grant is **equivalent** when it matches on user, acquisition, type and range, and is live (`expire_at` unset or
in the future). `POST /api/v1/user-acquisitions` (personal) returns an equivalent live grant if one exists (`303` as
today), otherwise it creates one. A different `range` creates a new grant; an expired grant is never returned
(#215); shared grants are links and always new. `AcquisitionDownload` reuses the reader's live whole-book grant.

`EVILFLOWERS_USER_ACQUISITION_MODE` is **removed** (Q10): no instance needs one grant per open, and reader-scoped
copies remove the cost `multiple` had. A deployment that still sets it gets a warning at start.

Revoking a grant (Q8): `DELETE /api/v1/user-acquisitions/<id>` sets `expire_at` to now (the holder, a manager of
the entry's catalog, a superuser; `204`). The row, its annotations (`Annotation.user_acquisition` cascades on a
real delete) and its history stay. Catalog membership is not a revocation source (Q1): a live grant stands on its
own until it expires or is revoked.

### B. Copies belong to the reader, not to the grant

New setting `EVILFLOWERS_PERSONALISED_COPY_SCOPE`:

- **`reader`** (default, Q2).
  - The copy is keyed on (reader, acquisition, range, `?page=`, annotations drawn, render version), not on the
    grant, and stored in `personalised/<user id>/` (Q3).
  - The watermark prints the reader (name and id, as before) and a **copy id** (the copy's digest) instead of the
    grant id. "Generated at" stays the render time.
  - Every open of the same book by the same reader reuses one stored copy.
  - The reader of a shared grant is whoever signed in opens it (Q6); anonymous recipients keep the per-request
    render with the owner's data.
  - `?annotations=true` draws the reader's annotations on every grant of this acquisition **with the same page
    range** (Q7; annotation pages index the generated document, so other ranges would land on the wrong pages).
- **`grant`**: the behaviour before IP-036. The copy and the watermark are per grant, for deployments that need
  every open traceable from the printed page alone.

Forensics under `reader`:
- A leaked copy names the reader (name, id) and the copy id. `ENTRY_DOWNLOADED` records grant and copy id of the
  reader's latest open (activity rows collapse repeats, so it is a hint, not a ledger).
- What is lost is identification of *which open* from the file alone.

One module decides all of this for both the view and the pre-render task: `apps/files/copies.py`.

### C. A range path without the database, with immediate revocation

**Pin v2.** The pin written by the first request of a reading session (`personalised.pin`) also stores the
authorisation facts that the range checks need:

```python
{
    # served (#214)
    "name": ..., "copy": True, "mime": ..., "filename": ..., "size": ..., "etag": ..., "last_modified": ...,
    "pinned_at": 1763520000.0,
    # IP-036, the owner's pin only
    "access": {
        "owner_id": "<user uuid>", "entry_id": "<uuid>", "expire_at": 1763520000.0 | None, "ip_block": True,
        "tokens": {"grant": 4119…, "user": 2870…, "entry": 913…},
    },
}
```

The owner's pin is keyed by grant and query only, so the fast path can find it knowing nothing else; another
reader of a shared grant has their own pin. A pin lives 15 min after its last read and **2 h at most**.

**Revocation tokens** (`apps/files/revocation.py`), `revocation:<ua|user|entry>:<id>` in the cache:
- A token is a random 62-bit number, never a counter. Revoking sets a new random value.
- A missing token means "unknown" (Q4): the range takes the full path, which mints a new one and re-pins. Eviction
  or a flush costs speed, never safety.
- No race: when pinning, the tokens are read (or minted) **first** and the grant's facts re-read after; a revocation
  that commits in between leaves a pin that is stale at once. Revocations run `on_commit`.
- Every save or delete of a `UserAcquisition`, `User` or `Entry` revokes (no field diffing). Users are saved at login
  and on account changes; reads save neither users nor entries (#214), so a revocation costs at most one full-path
  range. Catalog membership is not a source (Q1).
- `QuerySet.update()` sends no signal. The code base has no such update of grants, users' `is_active` or
  entries' IP block; `apps/content/services.py` updates only `content_policy`, which downloads do not read.

**The fast path** (`UserAcquisitionDownload.dispatch`, before `SecuredView.dispatch`). It applies to `GET` with
`Range` or `HEAD`, carrying a valid signed URL (Q5: bearer tokens keep the full path), not base64:

1. Verify the HMAC. No user `SELECT`.
2. Read the owner's pin (and touch it and the copy marker), then the three tokens (four cache round trips).
3. Fall back to the full path when the pin has no access facts, a token is missing or changed, `expire_at` has
   passed or the IP block refuses; the full path answers with its usual checks (`410`, `403`).
4. Serve the bytes with the pinned validators.

The result is **zero database queries per range** (tested), no rate-limit hit and no authentication backend.

### D. Ranges are not separate requests in the accounting

- A range served by the fast path does not count against the rate limit (it returns before `SecuredView.dispatch`):
  the open that created the pin already did. Full-path ranges still count.
- Its server span carries `evilflowers.files.fast_path=true`; no domain spans run.

### E. Render before the first GET

`EVILFLOWERS_PERSONALISED_PRERENDER` (default **on**, Q9):
- A new personal grant of a PDF queues `prerender_personalised_copy` (job kind `files.personalised_prerender`) after
  commit, with a **15 s countdown**: a GET that follows the grant at once takes the render lock itself and the task
  then finds the copy ready; a later GET finds it rendered.
- `AcquisitionDownload` creates grants it redirects to at once, so it never queues one; shared grants are not
  rendered ahead.
- Best effort: a broker outage only means the first GET renders, as before.

## Implementation Plan

### Phase 0: prerequisite
- [x] #215: enforce `expire_at` everywhere, and reuse only live grants (closed 2026-11-18).

### Phase 1: equivalent grant reuse (A)
- [x] `UserAcquisition.objects.live()` and `equivalent(user, acquisition, type, range)` (oldest first).
- [x] Used in `POST /api/v1/user-acquisitions` and `AcquisitionDownload`; `EVILFLOWERS_USER_ACQUISITION_MODE`
      removed, with a start-up warning when still set.
- [x] `DELETE /api/v1/user-acquisitions/<id>` = expire now (Q8).
- [x] Tests: reuse, another range = another grant, shared always new, `AcquisitionDownload` reuse, delete rights and
      kept annotations.
- [x] Docs: `docs/catalog-wiki/Entries.md`, `Settings-&-Enumerations.md`, `.env.example`.

### Phase 2: reader-scoped copies (B)
- [x] `EVILFLOWERS_PERSONALISED_COPY_SCOPE` (`reader` | `grant`), `apps/files/copies.py`.
- [x] `copy_name(…, reader=)` keys on the reader; the context `id` is the copy digest; the licence template wording
      "Document ID" stays. Authors in entry order (deterministic name).
- [x] The copy name changes for every copy (new key), so copies re-render once after the deploy.
- [x] Tests: one reader's grants share one copy and render once; three readers get three copies; `grant` scope
      keeps per-grant copies; the licence page carries reader and copy id; shared grants are watermarked with the
      requester; annotations of the same range only; the task and the view name the same copy.
- [x] Docs: `docs/files/streaming.md`.

### Phase 3: database-free range path (C)
- [x] `apps/files/revocation.py` (random tokens, `mint`, `revoke` on commit, `matches`) and receivers on
      `UserAcquisition` save/delete, `User` save/delete and `Entry` save.
- [x] Pin with access facts and tokens, 2 h maximum age; tokens read before the facts are re-read (no race); a
      stale pin is re-pinned by the full path.
- [x] Fast path in `UserAcquisitionDownload.dispatch` for signed URLs only.
- [x] Bulk paths audited (see C).
- [x] Tests: a signed range and a `HEAD` run **0** queries; revoke (DELETE), user deactivated and IP block on each
      refuse the next range; a pinned session ends with the grant's expiry; a missing token takes the full path and
      the next range is fast again; an over-age pin is dropped; facts that moved while pinning are not pinned;
      bearer ranges always query.

### Phase 4: range accounting (D)
- [x] Fast-path ranges never reach the rate limit; full-path ranges still count.
- [x] Fast-path spans: `evilflowers.files.fast_path=true`, no domain spans.

### Phase 5: pre-render (E)
- [x] `EVILFLOWERS_PERSONALISED_PRERENDER` (on), task `prerender_personalised_copy` (job kind
      `files.personalised_prerender`), queued after commit with a countdown.
- [x] Tests: the GET after a pre-render does not render; a new personal grant queues it after commit with the
      countdown; shared grants and `AcquisitionDownload` do not.

### Phase 6: measure and roll out
- [ ] Dev (knowledge#88): queries and duration per range from Tempo before and after, renders per open, storage
      under `personalised/`.
- [ ] Remove `EVILFLOWERS_USER_ACQUISITION_MODE` from the instances' env (knowledge#88).

## Technical Details

**Copy name under `reader` scope** (`personalised.copy_name(…, reader=…)`).
```python
key = {
    "render": _render_version(),
    "scope": "reader", "reader": str(reader.pk),
    "modifier": ..., "source": ..., "size": ...,
    "range": user_acquisition.range, "page": page, "annotations": ..., "context": context_without_grant_id,
}
```
- Storage path: `personalised/<user_id>/<digest>.pdf`; `cleanup_idle` is unchanged.
- `grant` scope keeps `personalised/<ua_id>/<digest>.pdf`.

**Fast-path preconditions.** All must hold, otherwise the full path runs:
- method `GET` with `Range`, or `HEAD`;
- not `format=base64`;
- a valid signed URL (HMAC, `exp` in the future);
- the owner's pin for (grant, variant) with access facts;
- all three tokens present and equal to the pinned ones;
- `expire_at` not passed, IP block passed.

**Settings summary.**

| Setting | Default | Meaning |
|---|---|---|
| ~~`EVILFLOWERS_USER_ACQUISITION_MODE`~~ | — | removed; equivalent live grants are reused |
| `EVILFLOWERS_PERSONALISED_COPY_SCOPE` | `reader` | `grant` = copy and watermark per grant (before IP-036) |
| `EVILFLOWERS_FILES_RANGE_FAST_PATH` | `1` | `0` = every range takes the full path (rollback switch) |
| `EVILFLOWERS_PERSONALISED_PRERENDER` | `1` | render a new personal grant's PDF copy before its first GET |

**Test isolation found on the way.** `Acquisition.content` builds its storage at import, so `override_settings` of
the data directory never reached it: the #70/#214 download tests wrote personalised copies into the real
`data/evilflowers/storage/personalised/` (307 files locally on 2026-11-18). The download test classes now patch the
field's storage to their temporary root; other test modules that render copies may still leak.

## Alternatives Considered

- **Keep the mode as the only switch.** Simple, but `multiple` stays expensive (a render per open), `single` keeps
  the range bug, and nothing separates forensics from performance.
- **Shorter pin TTL instead of revocation tokens** (for example 60 s). This needs no signals, but revocation is
  delayed by up to the TTL, and the database cost comes back once a minute per reader. The tokens cost one small
  Redis key per object read through the fast path and the discipline of calling `revoke()` after `update()`.
- **Generation counters** (the first draft) instead of random tokens: an evicted counter reads 0 again and can match
  an old pin (Q4).
- **Cache the `UserAcquisition` row in Redis** instead of pinning facts. The same invalidation problem, and a larger
  value.
- **Bearer tokens on the fast path:** rejected (Q5); the bearer backend refuses users for reasons no signal covers.
- **Per-open forensics via a per-request watermark.** That is the anonymous path today: a render per request, no
  ranges. Rejected for signed-in readers.
- **nginx offload.** Rejected in #214: the checks run per range anyway.

## Trade-offs and Risks

- **A missed `revoke()` keeps access alive** for the fast path until the pin ages out (15 min idle, **2 h at most**).
  Mitigations:
  - receivers on every save/delete of the three models (no diffing);
  - an audit of `QuerySet.update()` (2026-11-18): the only access-relevant hit, the admin's "Deactivate selected
    users", now revokes explicitly;
  - a test per revoking action, and the `EVILFLOWERS_FILES_RANGE_FAST_PATH=0` switch.
- **A cache outage while revoking** is logged, never raised (`on_commit(robust=True)`), so it cannot break logins or
  edits. The revocation is then late by at most the 2 h cap. This is a deliberate trade: availability of writes
  over seconds-exact revocation during a Redis outage.
- **`reader` scope weakens per-open forensics.** The file identifies the reader, not the open. Deployments that need
  more use `grant`.
- **Pre-render is on** (Q9): it can render copies nobody opens (storage until idle eviction, worker CPU). The 15 s
  countdown keeps it out of the way of a GET that follows at once.
- **A live grant stands on its own** (Q1): removing a reader from a catalog does not end their grants; cutting them off
  takes revoking the grants and removing the membership.
- **One-time re-render** after deployment: every reader's first open renders once, as after #214.

## Success Criteria

- A signed range of a pinned session runs 0 database queries (test ✅), and its p50 duration on dev drops measurably
  versus #214 (Tempo, Phase 6, open).
- A second open of the same book by the same reader, through any of their grants, renders 0 times (test ✅; spans on
  dev open).
- Every revoking action refuses the next range request (tests ✅).
- A personal `POST` returns a new grant for a different `range` and never an expired one (tests ✅).

## Future Considerations

- Moving annotations from the grant to (user, acquisition), so they survive new grants. This is out of scope here,
  but `reader` scope makes it natural.
- ASGI (see IP-032, rejected in favour of ASGI): the fast path is a natural async view.
- Applying the same pin and generation model to EPUB resources, if Readium Web ever reads EPUB through the catalog.

## References

- EvilFlowersCatalog#214 (range contract, shared base, pin v1), #215 (`expire_at`), #70 (stored copies).
- `apps/files/views.py`, `apps/files/personalised.py`, `apps/files/signing.py`, `apps/core/checkers.py`,
  `apps/api/views/user_acquisitions.py`, `docs/files/streaming.md`.
- knowledge#88 (rollout and measurement of #214).

## Discussion

Discussion on 2026-11-18 (streaming review):
- Copies, not bytes, are what should be cached (#214).
- The mode setting is the wrong knob for performance.
- Revocation must not depend on a TTL.

## Review Questions

**Status**: ✅ Answered (2026-11-18, interactively; plus a follow-up on Q8: revoke by expiring, never cascade)
**Review Date**: 2026-11-18
**Reviewer**: Claude AI

The following questions were answered before implementation:

---

### Q1: Does a grant survive the reader losing access to the catalog? 🔴 Critical

**Issue**:
- Proposed Solution C and Phase 3 bump generations on catalog membership changes, so the fast path would refuse
  the next range.
- But today's full path (`UserAcquisitionDownload`) never re-checks `check_entry_read`; it checks only the grant
  (owner, type) and the IP block.
- The fast path would therefore be stricter than the full path. A reader removed from a catalog would be refused
  on pinned ranges but would still download on a new open.

**Context**: The grant is the access decision made when it was created. Re-checking catalog membership on every
download changes what a grant means, and adds the membership query back to the full path.

**Question**: Should downloads re-check catalog read access, or does a live grant stand on its own until it
expires or is deleted?

**Options**:
- [ ] **A**: A live grant stands on its own. Drop catalog membership from the revocation list, so both paths agree.
      Removing access means deleting or expiring the grants (an admin action that calls `revoke()`). (recommended)
- [ ] **B**: Re-check `check_entry_read` on the full path too, and bump `gen:entry`/`gen:user` on membership changes.
- [ ] **C**: Keep today's inconsistency (not recommended).

**Answer**:
```
A — a live grant stands on its own (2026-11-18).
```

**Resolution**:
```
Catalog membership is not a revocation source. Downloads keep checking the grant (owner, type, expiry) and the
IP block only, on both paths. Removing a reader's access means deleting or expiring their grants, which revokes
at once.
```

---

### Q2: Default copy scope: `reader` or `grant`? 🔴 Critical

**Issue**:
- Under `reader` scope (Proposed Solution B, proposed default), the printed watermark identifies the reader and a
  copy id, not the individual open.
- The licence terms (`templates/files/license.txt`) and possibly the library's agreements with publishers may
  assume per-document identification.

**Context**: This is a policy decision. Performance favours `reader`, and so does storage in `multiple` mode.

**Question**: Which scope is the default, and does any deployment (STU, KU) require per-open identification?

**Options**:
- [ ] **A**: `reader` default; `grant` available per deployment. (recommended)
- [ ] **B**: `grant` default (today); `reader` opt-in.
- [ ] **C**: `reader` only; drop `grant`.

**Answer**:
```
A — `reader` default, `grant` available (2026-11-18).
```

**Resolution**:
```
`EVILFLOWERS_PERSONALISED_COPY_SCOPE=reader` by default; `grant` keeps today's per-grant copy and watermark.
```

---

### Q3: Storage layout of reader-scoped copies ⚠️ Medium

**Issue**:
- Technical Details proposes `personalised/u/<user_id>/<digest>.pdf` and says the `cleanup_idle` layout stays.
- `cleanup_idle` lists only one directory level under `personalised/` (one folder per owner, files inside). A
  `u/<user_id>/` level would hold directories, not files, and those copies would never be cleaned.

**Question**: Which layout?

**Options**:
- [ ] **A**: `personalised/<user_id>/<digest>.pdf`. User and grant ids are both UUIDs, so they cannot collide, and
      `cleanup_idle` is unchanged. (recommended)
- [ ] **B**: `personalised/u/<user_id>/…` and teach `cleanup_idle` two levels.
- [ ] **C**: A separate prefix (`personalised-reader/`) with its own cleanup pass.

**Answer**:
```
A — personalised/<user_id>/ (2026-11-18).
```

**Resolution**:
```
Reader copies: `personalised/<user_id>/<digest>.pdf`; grant copies keep `personalised/<ua_id>/…`. `cleanup_idle`
is unchanged.
```

---

### Q4: Generation counters and Redis eviction 🔴 Critical

**Issue**:
- Proposed Solution C treats a missing counter as 0.
- If Redis evicts a counter key (an `allkeys-lru` policy under memory pressure) after it was bumped from 0 to 1, it
  reads 0 again. A pin written at generation 0 then matches, and the revocation is missed.
- Pins and counters live in the same `default` cache.

**Context**: The cache and broker Redis on dev and prod: eviction policy not checked.

**Question**: How are counters protected from eviction?

**Options**:
- [ ] **A**: Counters never default to 0. A missing counter means "unknown", and the range takes the full path,
      which re-creates the counter at a fresh random base value. Eviction then costs speed, never safety.
      (recommended)
- [ ] **B**: Require a non-evicting policy (`volatile-lru` with counters stored without TTL). This depends on a
      deployment setting.
- [ ] **C**: Keep counters in PostgreSQL (one column per model) and cache them. This brings a query back.

**Answer**:
```
A — a missing counter means the full path (2026-11-18).
```

**Resolution**:
```
`revocation.current()` never defaults to 0. A missing counter makes the range take the full path, and the full
path re-creates the counter at a random 63-bit base before it pins. A pin then never matches a counter that was
evicted and re-created.
```

---

### Q5: Bearer tokens on the fast path ⚠️ Medium

**Issue**:
- Proposed Solution C lets a bearer request skip the user `SELECT` when the JWT subject equals the pin's owner.
- Today the bearer backend also resolves API keys and may reject a user for reasons other than `is_active` (for
  example a revoked API key, or an auth source that was disabled).
- Not every such event bumps `gen:user`.

**Question**: Do bearer tokens take the fast path, or only signed URLs?

**Options**:
- [ ] **A**: Signed URLs only. The portal uses `stream_url` (elvira-portal#63), and bearer ranges keep the full path.
      (recommended)
- [ ] **B**: Bearer too, after auditing every reason the backend can refuse a user and bumping `gen:user` for each.

**Answer**:
```
A — signed URLs only (2026-11-18).
```

**Resolution**:
```
Only a request carrying a valid `sig`/`exp` takes the fast path; bearer-token ranges keep the full path.
```

---

### Q6: Shared grants under `reader` scope ⚠️ Medium

**Issue**:
- A `shared` grant is readable by anyone, and today its copy and watermark carry the grant owner's name, not the
  reader's.
- Under `reader` scope "the reader" would have to mean the grant owner (the copy is shared by every recipient) or
  the requester (one copy per recipient, watermarked with them).

**Question**: Whose name goes on a shared copy?

**Options**:
- [ ] **A**: The requester's when signed in (one copy each), the owner's for anonymous recipients (rendered per
      request, as today). (recommended)
- [ ] **B**: The owner's, as today; one shared copy.

**Answer**:
```
A — the requester's when signed in (2026-11-18).
```

**Resolution**:
```
Under `reader` scope the copy's reader is the signed-in requester (their name, id, copy and folder); anonymous
recipients of a shared grant keep today's per-request render with the owner's data.
```

---

### Q7: Annotations drawn into a reader-scoped copy ℹ️ Low

**Issue**: With `?annotations=true` the view draws the annotations of the current grant. Under `reader` scope, one
copy serves all of a reader's grants for that acquisition.

**Question**: Draw this grant's annotations (a copy per grant whenever annotations are drawn), or the reader's
annotations on this acquisition across grants?

**Options**:
- [ ] **A**: The reader's annotations across their grants of this acquisition. This is consistent with the scope,
      and the annotations list filter already supports `user_acquisition__user`. (recommended)
- [ ] **B**: Only this grant's annotations.

**Answer**:
```
A — the reader's annotations across grants (2026-11-18).
```

**Resolution**:
```
With `?annotations=true` under `reader` scope, the drawn items are those of every annotation of the reader on any
of their grants of this acquisition.
```

---

### Q8: Missing `DELETE /api/v1/user-acquisitions/<id>` ⚠️ Medium

**Issue**:
- The portal's admin access-grants screen calls `DELETE /api/v1/user-acquisitions/<id>`
  (`elvira-portal` `useAdminAccessGrants.tsx`).
- `UserAcquisitionDetail` defines only `get`, so the call answers `405` unless another route handles it (not found
  on 2026-11-18).
- Revocation (Proposed Solution C) needs a working delete that calls `revoke()`.

**Question**: Add the endpoint in this proposal or in its own issue?

**Options**:
- [ ] **A**: Its own small issue (admin or owner may delete; `revoke()`; an activity record), done before Phase 3.
      (recommended)
- [ ] **B**: Part of Phase 3.

**Answer**:
```
B — within IP-036 (2026-11-18).
```

**Resolution**:
```
Phase 3 adds `DELETE /api/v1/user-acquisitions/<id>`: the holder, a manager of the entry's catalog, or a
superuser may delete; the delete revokes at once (expire now, follow-up answer) and is logged
(`user_acquisition.revoked`); no activity row, since activity history is the reader's own.
```

---

### Q9: Defaults of the fast path and pre-render ℹ️ Low

**Issue**: Settings summary proposes `EVILFLOWERS_FILES_RANGE_FAST_PATH=1` and
`EVILFLOWERS_PERSONALISED_PRERENDER=0`.

**Question**: Ship the fast path on by default, or off until dev measurements (Phase 6) confirm it?

**Options**:
- [ ] **A**: On by default, with the `0` switch as rollback. Correctness is covered by the Phase 3 tests.
      (recommended)
- [ ] **B**: Off by default; turn it on per instance after measuring.

**Answer**:
```
A for the fast path (on, with the off switch). Pre-render: build it, default ON (2026-11-18).
```

**Resolution**:
```
`EVILFLOWERS_FILES_RANGE_FAST_PATH=1` and `EVILFLOWERS_PERSONALISED_PRERENDER=1` by default. Pre-render queues
the copy of every new personal grant of a PDF when no copy exists yet; it is best effort (a broker outage only
means the first GET renders, as today).
```

---

### Q10: Is `multiple` mode used anywhere? ℹ️ Low

**Issue**:
- Which mode dev, KU and prod run is not recorded in the knowledge base or `.env.example`; the default is `single`.
- If no deployment uses `multiple`, Proposed Solution A could drop it instead of keeping two modes.

**Question**: Which mode does each instance run, and is `multiple` still needed?

**Options**:
- [ ] **A**: Keep `multiple` for per-issue auditing. (recommended until the instances are checked)
- [ ] **B**: Remove `multiple`; equivalent reuse only.

**Answer**:
```
B — remove `multiple` (2026-11-18).
```

**Resolution**:
```
`EVILFLOWERS_USER_ACQUISITION_MODE` is removed. Every request reuses an equivalent live grant (user,
acquisition, type, range) or creates one. A deployment that still sets the variable gets a warning at start.
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-11-18 | jdubec | Initial draft (AI-assisted) from the #214 streaming review |
| 2026-11-18 | jdubec | Added Review Questions section (Q1–Q10) |
| 2026-11-18 | jdubec | Q1–Q10 answered; Q8 follow-up: DELETE expires instead of deleting (annotations cascade); status Accepted |
| 2026-11-18 | jdubec | Phases 0–5 implemented and tested locally (uncommitted); body synced with the answers: mode removed, random revocation tokens, owner-keyed pins with a 2 h cap, pre-render with countdown; test storage isolation noted |
| 2026-11-18 | jdubec | Revocation drops tokens with robust on-commit hooks (a cache outage never breaks a save); `update()` audit: admin user deactivation now revokes; Trade-offs, Alternatives and Success Criteria synced |
