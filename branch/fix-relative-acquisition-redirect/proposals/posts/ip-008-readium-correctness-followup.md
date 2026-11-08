---
draft: false
date: 2026-05-25
authors:
  - jdubec
categories:
  - Feature
tags:
  - readium
  - dataverse
  - lcp
  - opds
  - search
  - consolidation
---

# IP-008: Readium + Dataverse Post-Merge Consolidation

The Readium LCP (IP-001, IP-003, IP-004) and Dataverse + text-service (PR #54)
merges introduced two large subsystems that now need a consolidation pass: the
Readium module has concurrency, LSD/RWPM-conformance, and lifecycle bugs that
survived IP-003 and IP-004; the Dataverse integration lives as an 800-line god
view inside `apps/api/` with no tests, daemon-thread workflow resume, and
multi-tenant routing that silently picks "first catalog alphabetically"; OPDS
1.2 emits no LCP license link at all, blocking Thorium and other
Readium-toolkit readers from importing LCP titles from personal feeds; and the
already-deployed search service is wired for indexing on upload but never
consulted by any OPDS search endpoint. This proposal is the executable
consolidation: make Readium correct, lift the Dataverse integration into its
own Django app with Celery-based workflow resume + real multi-tenant routing,
emit LCP links from OPDS 1.2, and connect the search service to OPDS 2.0
search.

<!-- more -->

## Status

**Status**: Implemented
**Last Updated**: 2026-05-25
**Implementation**: Complete — all six phases shipped on top of IP-007
(commit `1953da4`).

## Problem Statement

The May 2026 post-merge audit produced four coherent clusters of work that
together determine whether the catalog reaches a functional, deployable state
for the next few months. They are bundled here because each cluster
cross-references the others (Readium correctness needs the Dataverse-side
`storage_backend` enum; OPDS 1.2 LCP parity needs `BorrowLinkResolver`
shared with OPDS 2.0; the search-service wiring needs the Dataverse-side
indexing call to be exactly-once).

### Baseline assumed (IP-007 already landed)

This proposal builds on commit `1953da4` (IP-007 — Crash-on-First-Contact
Bug Triage). The following are now in `develop` and are NOT in scope here:

- `License.unique_together` replaced with partial
  `UniqueConstraint(fields=["entry","user"], condition=Q(state__in=
  ["ready","active"]))` (migration
  `apps/readium/migrations/0006_alter_license_unique_together_and_more.py`).
  Phase 1 A1 below layers `transaction.atomic()` + `select_for_update`
  on top.
- Readium signal name-mangling fixed (`_original_state` / `_original_status`
  / `_original_passphrase_hash` with single underscore).
- `Entry.first_author_name` property — the canonical author-display
  helper. Phase 3 C2 expects it.
- `DetailType.FORBIDDEN` and `DetailType.INTERNAL_ERROR` exist in
  `apps/core/errors.py`.
- `apps/opds/services/entry_search.py::EntrySearchService.search(catalog,
  request)` — the shared catalog-DB search layer consumed by both OPDS
  1.2 (`apps/opds/views/search.py::SearchView`) and OPDS 2.0
  (`apps/opds2/views/search.py`). Phase 6 F1 extends THIS service with
  `mode=keyword|semantic` dispatch.
- `OpenSearchDescription` schema + URL-encoded template in
  `apps/opds/schema.py`. Phase 6 just advertises the `mode` parameter
  through it.
- `apps/api/utils/parse.py::parse_int_query` — the safe query-int
  parser; OPDS 2.0 views already consume it.
- `DV_BASE_INTERNAL` env name is now consistent across `compose.yml` and
  `apps/api/views/dataverse.py`.
- The duplicate text-service Celery enqueue at
  `apps/api/views/entries.py` is removed; one publish per PDF upload.
- OPDS 1.2 root feed query uses `parents__isnull=True`; Latest feed
  sorts `-created_at`.
- `license_renewed` notification fires from `LicenseService.renew_license`;
  `NotificationLog.NotificationType` enum lists all 9 active types.
- `Category` uniqueness + `Feed` permission scope fixes
  (IP-007 D17/D18) — IP-010 builds on the same baseline.

### Cluster A — Readium concurrency

**A1. `LicenseService.create_license` is not atomic.**
- `apps/readium/services/license_service.py:202-313`. `can_user_borrow`
  performs an unlocked count; the body of `create_license` is not wrapped in
  `transaction.atomic()`; no `select_for_update` on the `Entry`.
- Two concurrent borrow requests on the same entry both see "1 slot free" and
  both create licenses, blowing past `readium_amount`.
- IP-004 added `over_saturated` visibility precisely because this can happen;
  the write path still doesn't prevent it.

**A2. `ReservationService.promote_next` over-promotes.**
- `apps/readium/services/reservation_service.py:148-184`. The query takes
  `select_for_update(skip_locked=True)` on the single head reservation row,
  but reads `active_count + pending_promotions` over the entry's licenses
  outside the lock.
- Two parallel returns can both see "1 slot free" and promote two different
  reservations.

**A3. `expire_unclaimed` nested transaction over-locks.**
- `apps/readium/services/reservation_service.py:196-217`. The sweep is
  `@transaction.atomic`; `promote_next` (line 137) is also
  `@transaction.atomic`. Django re-uses the outer transaction as a savepoint,
  so the `select_for_update(skip_locked=True)` in `promote_next` skips rows
  the outer query locked — promotion never happens for entries the sweep
  touched.

**A4. Status-Server `return_license` and `renew_license` skip explicit PATCH.**
- `apps/readium/services/status_server_client.py:92-145`. The methods update
  local state and call `LCPServerClient`, but do not issue `PATCH
  /licenses/{id}/status` to the Status Server with the new state.
- LCP LSD spec §5.1 requires the Status Server be the canonical state
  surface; reader apps that re-fetch the status document see the old state
  until something else triggers the LSD update.

**A5. Encryption status flips REGISTERED before encryption finishes.**
- `apps/readium/services/content_encryption_service.py:89-94`. The optimistic
  write marks status `REGISTERED` immediately. `is_ready_for_licensing`
  returns True, so `LicenseService.create_license` happily issues licenses
  against content the LCP Server hasn't seen yet. The webhook
  (`apps/readium/views/hooks.py:65`) was supposed to flip the status; today
  the status is already flipped by the time the webhook arrives.

**A6. `apps/readium/views/licenses.py:160-163` state mutation outside atomic.**
- `_handle_state_change` writes `license.state = ACTIVE; device_count += 1`
  then `form.populate(license)` then `license.save()`, all without a
  transaction. Race with the status proxy device-registration loses
  `device_count` increments.

### Cluster B — LSD / RWPM Spec Conformance

**B1. Content download lacks LCP MIME and Cache-Control header.**
- `apps/readium/views/content.py:62-66` serves the encrypted file with the
  plaintext `acquisition.mime` (`application/pdf` / `application/epub+zip`).
  LCP-protected files must advertise `application/pdf+lcp` and
  `application/audiobook+lcp+json` per the LCP for PDF / Audiobook profiles.
- `apps/readium/tests/test_lcp_compliance.py:10` claims a `Cache-Control`
  test exists. It does not — no `Cache-Control` header is set anywhere in
  the view.

**B2. Status proxy link rewriting is rel-based, not host-based.**
- `apps/readium/views/status_proxy.py:38` rewrites only `register / license /
  return / renew / hint` rels. LSD also emits `status`, `publication`,
  `self` — these go un-rewritten and leak internal `127.0.0.1` URLs to the
  client.

**B3. `lcp_ext_map` accepts audiobook MIME that has no enum value.**
- `apps/readium/services/content_encryption_service.py:63-68`. Maps
  `application/audiobook+zip` but `AcquisitionMIME`
  (`apps/core/models/acquisition.py:41-45`) has no `AUDIOBOOK` option. The
  branch is unreachable; drop it.

**B4. Hint page is enumerable.**
- `apps/readium/views/hint.py:16-29` — anonymous users can enumerate license
  UUIDs and read passphrase hints. Hint is a hint, not a secret, but
  enumeration is undesirable. Return the same response shape for "no hint
  set" and "license not found" + rate-limit.

**B5. OPDS 1.2 emits no `application/vnd.readium.lcp.license.v1.0+json` link.**
- `apps/opds/schema.py::AcquisitionEntry.from_model` carries direct-download
  acquisition links only. OPDS 2.0 (`apps/opds2/views/borrow.py:72-74` and
  `apps/opds2/services/feed_builder.py:196`) correctly emits the LCP link;
  OPDS 1.2 leaks the raw file URL even for LCP-protected content and
  Thorium / other Readium-toolkit readers cannot import LCP titles from
  OPDS 1.2 personal feeds.

### Cluster C — Service / View Correctness

**C1. License creation re-issues to LCP server with first-found PDF/EPUB
only.**
- `apps/readium/services/license_service.py:266-280`. If an entry has both
  an EPUB and a PDF, only one gets a license; the choice depends on
  QuerySet ordering. Deterministically pick (or require caller to specify).

**C2. `License.objects.create(..., state=READY)` fires the post-save signal
before LCP issuance.**
- `apps/readium/services/license_service.py:283-292`. The notification
  signal (`apps/notifications/signals.py:8`) sends a "your license is
  ready" email before the LCP Server has actually issued the license. If
  issuance fails and `license.delete()` runs (line 310-313), the email
  already went out.

**C3. `LCPServerClient.generate_license` double-save with passphrase hash.**
- `apps/readium/services/lcp_server_client.py:92-94` saves the license with
  `passphrase_hash`, then posts to LCP server, then saves again with
  `lcp_license_id`. If the POST fails, the row already persists the hash.
  Use a single transactional save after success.

**C4. `User.lcp_passphrase_hash` case mismatch can lock users out.**
- `apps/readium/services/lcp_server_client.py:47, 85` uppercase fresh
  hashes; legacy `User` rows from before the `.upper()` change carry
  lowercase hashes. `fetch_fresh_license` sends them as stored; LCP server
  rejects. Normalize on `User.save()` and in a one-shot data migration;
  defensively `.upper()` at the client.

**C5. `_maybe_promote_next` swallows all exceptions.**
- `apps/readium/services/license_service.py:443-460` — `try: ... except
  Exception: logger.exception(...)`. If the queue is broken, no metrics,
  no surfacing.

**C6. Status proxy double-write to LSD.**
- `apps/readium/views/status_proxy.py:131-133` calls
  `StatusServerClient.return_license` after having already proxied the
  upstream PUT `/return`. `return_license` then does another save and
  another PATCH-via-LCPServerClient. Simplify the flow.

**C7. Renewal end-date rounded to whole days.**
- `apps/readium/views/licenses.py:283` — `days = max(1, int((decision.new_end
  - timezone.now()).total_seconds() // 86400))` discards the exact
  `requested_end` and re-derives. Either accept exact datetimes through
  `LicenseService.renew_license` or stop accepting them at the API.

**C8. Encryption re-encrypt does not delete orphan file.**
- `apps/readium/views/encryption.py:152-166`. `force=True` deletes the
  `EncryptedContent` row but not the encrypted file on disk/S3. Storage
  grows indefinitely on every force.

### Cluster D — Dataverse: god view, daemon-thread resume, no routing

**D1. 823-line god view with no tests.**
- `apps/api/views/dataverse.py` is 823 lines, contains a 500-line god
  method `DataversePrepublishIngest.post()` with seven nested closures
  (`extract_metadata`, `flatten_text`, `first_text`, `parse_partial_date`,
  `resolve_language`, `build_content`, `build_citation`), mixed concerns
  (auth, two upstream HTTP fetches, parsing, DB upsert, author syncing,
  acquisition creation, thread-spawning workflow resume), and no tests.
- The integration is a B2B webhook + sync flow, not the catalog's customer
  REST API. Auth flavour, route shape, and operational characteristics
  differ.
- `apps/api/services/text_service_client.py` has no state; `__init__` is a
  no-op `pass`. It belongs as a stateless module or in a separate
  text-processing app.
- `DataverseSync` (`apps/api/views/dataverse.py:219-242`) is wired at
  `/api/v1/dataverse-sync` but no Dataverse workflow ever calls it (no
  `postpublish-sync.json` exists). Dead route.

**D2. Workflow resume on a daemon thread.**
- `apps/api/views/dataverse.py:202-214` spawns a `threading.Thread` daemon
  from a request handler for workflow resume. Under gevent gunicorn workers
  (`conf/gunicorn.conf.py:4`) this becomes a green thread that dies if the
  worker is recycled. The resume retries up to 10 times with 30s max
  backoff (~5 minutes), well past Gunicorn `timeout=240`.
- No retries surface in any tracker; observability is one log line per
  attempt.

**D3. Multi-tenant catalog routing is documented but unwired.**
- `apps/api/views/dataverse.py:367-389` — the docstring describes a routing
  scheme (dataset_id map, global_id prefix, env override, default,
  fallback). The implementation is `Catalog.objects.order_by("id").first()`.
  Every published Dataverse dataset lands in catalog #1 regardless of
  source.
- `DATAVERSE_CATALOG_URL_NAME` is documented in the comment but not wired.
- No unique index on `(catalog, identifiers ->> 'dataverse_pid')`. Two
  concurrent prepublish callbacks for the same `global_id` race to upsert
  into `Entry`.

**D4. Asymmetric Dataverse sync.**
- `apps/api/views/dataverse.py:749` clears authors only when the new author
  list is truthy; never deletes acquisitions removed upstream (line 761-818
  is CREATE/UPDATE only).
- Dataverse-imported acquisitions are hardcoded to
  `AcquisitionType.OPEN_ACCESS` (`apps/api/views/dataverse.py:809`), even
  when the upstream file is `restricted`.

**D5. Configuration mismatches.**
- `compose.yml:57` `EVILFLOWERS_TEXT_SERVICE_URL` and `compose.yml:58`
  `TEXT_SERVICE_REDIS_URL` set on Django but read by no code.
- `dataverse/scripts/bootstrap-workflow.sh:33-35` hardcodes
  `http://dataverse:8080` instead of using `$DV_URL`/`$DV_BASE_INTERNAL`.
- `dataverse/compose.override.yml:2` overrides a service named `postgres`,
  but the catalog's DB service is `db`. Override targets a non-existent
  service.

(IP-007 D9 fixed the `DV_PUBLIC_INTERNAL` → `DV_BASE_INTERNAL` rename in
`compose.yml`; the env names are now consistent.)

### Cluster E — Storage polymorphism for `file_url`

**E1. The `file_url` field leaks across consumers.**
- `apps/core/models/acquisition.py` introduced a `file_url` field (commit
  `4b11e0b`) so a Dataverse-backed acquisition is `(file_url, no local
  content)`. The presence of two storage modes (local content via
  `apps/files/storage`, remote URL via redirect) leaks across every
  consumer of `Acquisition`. Dispatching is `if file_url else ...`
  everywhere.
- `apps/files/views.py:63-68` — the `file_url` redirect bypasses
  `check_entry_read`, `_check_ip_block`, and UserAcquisition creation.
  Functional bug + future security hardening item.
- `apps/core/models/acquisition.py:74-87` — `base64` and `checksum`
  properties read full file content on every access; `EntrySerializer.
  Detailed` serializes both, causing per-row file reads + SHA-256 on every
  detailed API response. Compounded by Dataverse-import volume.

### Cluster F — Search service: indexed but never queried

**F1. Search service is wired one-way only.**
- `evilflowers-text-service` and `evilflowers-search-service` index every
  PDF on upload (`apps/api/views/entries.py:269-289`, single after IP-007
  D10 fix).
- `apps/opds2/views/search.py` uses `EntryFilter` (catalog DB query on
  title/authors/categories). It does NOT consult the search service's
  keyword (`POST /search/elasticsearch`) or semantic
  (`POST /search/semantic`) endpoints.
- OPDS clients searching via `?query=...` see only catalog-DB matches,
  missing in-document keyword and semantic results that the search
  service indexes.

**F2. Text-service client is fragile.**
- `apps/api/services/text_service_client.py` has no timeout, retry config,
  idempotency key, queue-name override, or failure surface. If the worker
  queue is slow, `app.send_task` blocks the request handler.
- Search-service port: `compose.yml:147, 164-165` exposes
  `search-service:8001` but `docs/search_service_reference.pdf` documents
  the container listens on `8000`. Mismatch undocumented; works today by
  coincidence.

### Who is Affected

- **STU library staff under load** — concurrent borrows can over-issue
  licenses past the cap (A1, A2).
- **EDRLab `lcp-testing-tools`** — expects status doc to reflect
  return/renew immediately (A4); expects LCP MIME on encrypted content
  (B1); expects no internal-host leakage (B2).
- **Reader apps (Thorium, Readium-toolkit)** — cannot import LCP titles
  from OPDS 1.2 personal feeds (B5); fetch RWPM manifest and rely on
  `application/pdf+lcp` MIME (B1).
- **Library acquisitions team** — Dataverse-published datasets currently
  all land in one catalog (D3); multi-tenant rollout to other Slovak
  universities is blocked.
- **Operations** — workflow resume daemon-thread under gevent loses
  pending resumes on worker restart (D2).
- **Catalog API consumers** — every detailed entry response triggers a
  base64 + SHA-256 read of the entire PDF (E1).
- **OPDS search clients** — see catalog-DB matches only; in-document
  full-text and semantic search results never reach them despite indexing
  running on every upload (F1).

### Consequences of Not Addressing

- Production over-issues licenses under burst load, contradicting the
  contract IP-004 documents.
- A `RETURNED` license stays `READY` to readers for up to the LSD poll
  interval after the user returns it.
- Thorium / Readium-toolkit readers cannot use OPDS 1.2 personal feeds for
  LCP titles; STU has to maintain OPDS 2.0-only flows for them.
- The Dataverse integration is undeployable on more than one catalog
  without a fork.
- Worker restart during a publish loses the workflow resume.
- The search service indexes content nothing queries — wasted compute,
  wasted ES/Milvus storage.

## Proposed Solution

### Overview

Six phases, each independently shippable. Phases 1–3 are the Readium
correctness core. Phases 4–5 are the Dataverse extraction + polymorphic
storage. Phase 6 is the search-service wiring + OPDS 1.2 LCP link parity.
The phases share helpers (`attach_lcp_license_link`,
`AcquisitionStorageService`) introduced in the early phases and consumed by
later ones.

### Key Components

1. **Concurrency** — atomic license create + entry-level locking;
   reservation lock scope fix; correct nested-transaction semantics in
   `expire_unclaimed`; explicit Status Server PATCH on return/renew; defer
   status-flip to encryption webhook.
2. **LSD / RWPM compliance** — LCP MIME on encrypted content download;
   cache headers; host-based link rewriting in status proxy; LCP MIME
   enum normalization; rate-limit on hint page.
3. **Service correctness** — defer `license_created` notification until
   after LCP issuance succeeds; single-save in
   `LCPServerClient.generate_license`; deterministic acquisition
   selection; renewal end-date precision; orphan-file cleanup on
   re-encrypt; narrow exception handling in `_maybe_promote_next`.
4. **Extract `apps/dataverse/`** — new Django app, Celery-based workflow
   resume, real multi-tenant routing, symmetric upstream sync, unique
   constraint on `dataverse_pid`.
5. **Polymorphic storage backend on `Acquisition`** — `storage_backend`
   enum, `AcquisitionStorageService` for single-point dispatch, lazy
   `base64`/`checksum`.
6. **OPDS 1.2 LCP parity + search-service integration** —
   `attach_lcp_license_link` shared with OPDS 2.0; OPDS 2.0 search
   `?mode=keyword|semantic|catalog`; surface search-service down with
   `502 + Retry-After`.

### Architecture

```mermaid
flowchart TB
    subgraph CREATE[License Create — atomic]
        REQ[/POST /api/v1/entries/{id}/borrow/] --> LOCK[transaction.atomic + Entry select_for_update]
        LOCK --> COUNT[count active licenses]
        COUNT -->|< cap| LCP[POST to LCP server]
        LCP -->|ok| SAVE[(persist License<br/>state=READY<br/>notification on_commit)]
        LCP -->|fail| ABORT[(no DB row<br/>no notification)]
        COUNT -->|>= cap| REJECT[409 saturated]
    end
    subgraph DV[Dataverse pre-publish]
        DVP[POST /api/v1/dataverse-prepublish] --> SYNC[DataverseSyncService]
        SYNC --> ROUTE[CatalogRouter<br/>map → prefix → env → default]
        ROUTE --> UPS[EntryUpserter<br/>SELECT FOR UPDATE on dataverse_pid]
        UPS --> ENT[(Entry / Acquisition<br/>storage_backend=external_url<br/>file_url=...)]
        SYNC -.->|on_commit| WF[resume_workflow.delay]
        SYNC -.->|on_commit| TXT[publish_to_text_service.delay]
        TXT --> TS[(text-service)] --> SS[(search-service POST /index)]
    end
    subgraph SEARCH[OPDS 2.0 search]
        S[/GET /opds/v2/{c}/search?mode=…/] --> SV[SearchView]
        SV -->|catalog| EF[EntryFilter DB]
        SV -->|keyword| ESS[search-service /search/elasticsearch]
        SV -->|semantic| MS[search-service /search/semantic]
    end
    style LOCK fill:#cfc,stroke:#393
    style ABORT fill:#cfc,stroke:#393
    style ROUTE fill:#cfc,stroke:#393
    style UPS fill:#cfc,stroke:#393
    style WF fill:#cfc,stroke:#393
    style SV fill:#cfc,stroke:#393
```

## Implementation Plan

### Phase 1: Concurrency

- [ ] **A1** — Wrap `LicenseService.create_license` body in
  `transaction.atomic()`. Add
  `Entry.objects.select_for_update().get(pk=entry.pk)` at the top to
  serialize concurrent borrows on the same entry. Add tests with
  `transaction.on_commit` plus a parallel-pytest fixture that hits the
  endpoint with 5 concurrent threads and asserts only `cap` licenses are
  created.
- [ ] **A2** — In `ReservationService.promote_next`, replace the head-only
  `select_for_update(skip_locked=True)` with: `select_for_update()` on the
  Entry + counting active licenses + reservations under the lock; promote
  at most `available_slots` reservations in a single transaction.
- [ ] **A3** — Split `expire_unclaimed` into two passes: (1) lock + expire
  affected reservations and commit; (2) for each affected entry, call
  `promote_next` in its own transaction. Document in code why nested-atomic
  semantics necessitate the split.
- [ ] **A4** — `StatusServerClient.return_license` and `renew_license`
  (`apps/readium/services/status_server_client.py:92-145`) — issue `PATCH
  /licenses/{license_id}/status` with the new state to the Status Server.
  Update local state only after the PATCH succeeds.
- [ ] **A5** — `ContentEncryptionService.encrypt_acquisition`
  (`apps/readium/services/content_encryption_service.py:89-94`) — keep
  status `PENDING` until the webhook callback flips it to `REGISTERED`.
  Add a guard on `is_ready_for_licensing` requiring `encrypted_at is not
  None`.
- [ ] **A6** — `apps/readium/views/licenses.py:160-163` — wrap
  `_handle_state_change` body in `transaction.atomic()`.

### Phase 2: LSD / RWPM Compliance

- [ ] **B1** — `apps/readium/views/content.py:62-66`:
    - Map MIME via a new helper `_lcp_content_type(acquisition.mime)`:
      `application/pdf` → `application/pdf+lcp`, `application/epub+zip` →
      `application/epub+zip` (per Readium LCP, EPUB stays as-is and
      LCP-ness lives inside).
    - Set `response["Cache-Control"] = "private, no-store"`.
    - Add tests in `apps/readium/tests/test_lcp_compliance.py` exercising
      a real download and asserting both headers (replace the existing
      source-grep test).
- [ ] **B2** — `apps/readium/views/status_proxy.py:38` — change
  `_rewrite_links` to rewrite ANY link whose host matches the internal
  LSD host (configurable via `EVILFLOWERS_READIUM_LSDSV_INTERNAL_HOST`),
  regardless of `rel`. Add a unit test against a sample LSD body that
  includes `status`, `publication`, `self` rels.
- [ ] **B3** — Drop the `application/audiobook+zip` branch in
  `lcp_ext_map` (`content_encryption_service.py:63-68`). Re-introduce
  when a real audiobook proposal arrives.
- [ ] **B4** — `apps/readium/views/hint.py:16-29` — return the same
  response shape for "no hint set" and "license not found" to avoid
  enumeration. Add a `django-ratelimit`-style 1-req-per-second-per-IP
  throttle.
- [ ] **B5** — OPDS 1.2 LCP link emission (per Q4 resolution):
    - Introduce `apps/opds/services/borrow_link.py::BorrowLinkResolver`
      as the single dispatch point for borrow / LCP-license link
      emission. Both OPDS profiles consume it; format conversion (Atom
      `<link>` element vs RWPM JSON link object) stays local to each
      schema. See Q4 resolution for the resolver API and the link
      tuple it returns.
    - Refactor `apps/opds2/views/borrow.py:70-78` and
      `apps/opds2/services/feed_builder.py:193-201` (today's duplicated
      OPDS 2.0 emit) to consume `BorrowLinkResolver.emit_links`.
    - Update `apps/opds/schema.py::AcquisitionEntry.from_model`
      (OPDS 1.2) to call `BorrowLinkResolver.emit_links` and format
      each returned `Link` as an OPDS-1.2 `<link>` element. When
      `entry.readium_enabled` is True, emit:
        - `rel=http://opds-spec.org/acquisition/borrow`,
          `type=application/vnd.readium.lcp.license.v1.0+json` →
          OPDS 2.0 borrow endpoint (cross-version link).
        - if the user has an active license:
          `rel=http://opds-spec.org/acquisition`,
          `type=application/vnd.readium.lcp.license.v1.0+json` →
          `/readium/v1/licenses/{id}.lcpl`.
      The resolver suppresses the direct download link for
      readium-enabled entries (matching OPDS 2.0).
    - Add `apps/opds/tests/test_lcp_link_emission.py` asserting
      byte-for-byte parity between OPDS 1.2 and OPDS 2.0 on the
      `(rel, type, target_url)` tuple for the same entry.

### Phase 3: Service Correctness

- [ ] **C1** — `apps/readium/services/license_service.py:266-280` —
  change "first acquisition matching EPUB/PDF" to deterministic
  selection: prefer `mime=PDF` when both exist; require explicit
  override via a new `LicenseService.create_license(..., format="epub")`
  kwarg.
- [ ] **C2** — Defer the `license_created` notification to fire only on
  successful LCP issuance. Move the post-save side effect off
  `License.post_save` and onto an explicit `LicenseService.create_license`
  final step (`transaction.on_commit(lambda: send_notification.delay(...))`).
- [ ] **C3** — `apps/readium/services/lcp_server_client.py:92-94` —
  remove the first `license.save()`. Persist after success only. Wrap
  the whole `generate_license` flow in `transaction.atomic()`. Extract
  `_build_partial_license(license)` so it's shared with `fetch_fresh_license`.
- [ ] **C4** — `apps/core/models/user.py` — normalize
  `lcp_passphrase_hash` to uppercase in `save()` before write. Add a
  data migration that uppercases existing rows. In
  `LCPServerClient.fetch_fresh_license`, defensively `.upper()` again.
- [ ] **C5** — `apps/readium/services/license_service.py:443-460` —
  narrow the catch from `Exception` to specific exception classes
  (`requests.RequestException`, DB integrity errors). Surface unexpected
  exceptions; emit metrics on every catch.
- [ ] **C6** — `apps/readium/views/status_proxy.py:131-133` — proxy +
  read-after-write from LSD; the LSD is the canonical state, the local
  License row is a cache. Remove the duplicated local mirror call.
- [ ] **C7** — `apps/readium/views/licenses.py:283` — accept an exact
  `requested_end` datetime through `LicenseService.renew_license`;
  remove the day-rounding.
- [ ] **C8** — `apps/readium/views/encryption.py:152-166` — on
  `force=True`, delete the encrypted file from storage before deleting
  the `EncryptedContent` row. Wrap in `transaction.on_commit` so a
  failed file delete keeps the row.
- [ ] **D1 cleanup** — Move `LicenseChecker` from
  `apps/core/checkers.py:11` to `apps/readium/checkers.py`. Update
  `object_checker` configuration to discover both modules (setting:
  `OBJECT_CHECKER_MODULES = ["apps.core.checkers",
  "apps.readium.checkers"]`). Core ↔ readium directional inversion gone.
- [ ] **D2 cleanup** — Replace `apps/core/models/entry.py:138-157`
  synchronous encryption trigger with a domain event: `Entry.save()`
  calls `event_broker.execute("entry.changed", entry)` and a new
  `apps/readium/listeners.py` subscribes and enqueues
  `encrypt_acquisition` per acquisition. Guard `event_broker`
  resolution against missing `EVILFLOWERS_EVENT_BROKER_EXECUTOR` setting
  (mirrors the existing guard in `apps/core/models/acquisition.py:111`).
- [ ] **D3 cleanup** — `apps/opds2/views/borrow.py:18-83` — remove the
  duplicated passphrase pre-check; trust `LicenseService.create_license`
  to raise `PassphraseRequiredError` and map to RFC 7807.
- [ ] **D4 cleanup** — Extract `_get_license(check_name)` into a mixin
  used by `apps/readium/views/licenses.py` and
  `apps/readium/views/download.py`.

### Phase 4: Extract `apps/dataverse/`

- [ ] Create `apps/dataverse/` with `apps.py`, `urls.py`, `views.py`,
  `services/`, `tests/`, `migrations/` directory.
- [ ] Move `apps/api/views/dataverse.py` → split:
    - `apps/dataverse/views.py` — thin views: `PrepublishView`.
    - `apps/dataverse/services/client.py` — `DataverseClient` (HTTP
      client; timeouts, retries, structured errors).
    - `apps/dataverse/services/sync.py` — `DataverseSyncService`
      (orchestration: fetch, transform, upsert).
    - `apps/dataverse/services/mapper.py` — pure-function metadata
      mapping (the nested closures become module-level functions;
      testable in isolation).
    - `apps/dataverse/services/router.py` — `CatalogRouter` (the
      documented mapping; see Phase 4 routing tasks below).
    - `apps/dataverse/services/workflow.py` — Celery task interface for
      workflow resume.
    - `apps/dataverse/services/whitelist.py` —
      `_ensure_workflow_resume_ip_allowed`.
    - `apps/dataverse/forms.py` — Pydantic / Django-forms payload
      validator.
- [ ] Move `apps/api/management/commands/dataverse_prepublish_debug.py`
  → `apps/dataverse/management/commands/`.
- [ ] Update `INSTALLED_APPS` and URL routing: `apps/api/urls.py:119-120`
  keeps the two prepublish/sync route entries pointing at
  `apps.dataverse.views.PrepublishView` (shim) for one release.
- [ ] Move `apps/api/services/text_service_client.py` →
  `apps/dataverse/services/text_publish.py`.
- [ ] Delete `DataverseSync` (`apps/api/views/dataverse.py:219-242`)
  and its route. No Dataverse workflow calls it.
- [ ] Add `apps/dataverse/tests/` with unit tests for each service.
  Smoke test for the full prepublish flow with mocked `requests`.
- [ ] **D2** Celery task for workflow resume:
    - Define `apps/dataverse/tasks.py::resume_workflow(workflow_id,
      attempts_remaining, delay_seconds)`. `@shared_task(autoretry_for=
      (DataverseTransientError,), max_retries=10, retry_backoff=True,
      retry_backoff_max=30)`.
    - Replace the `threading.Thread(target=...)` with
      `transaction.on_commit(lambda: resume_workflow.delay(workflow_id,
      attempts, initial_delay))`.
    - Add a `JobProtocol` row for each resume attempt via the existing
      `celery_event_handler` infrastructure
      (`apps/tasks/management/commands/celery_event_handler.py`).
    - Test: simulate a worker restart between request handling and
      resume firing; assert the resume completes after restart.
- [ ] **D3** Multi-tenant catalog routing:
    - Implement `CatalogRouter` with the documented precedence:
        1. Explicit `dataset_id → catalog_url_name` map (JSON setting
           `EVILFLOWERS_DATAVERSE_CATALOG_MAP`).
        2. `global_id` prefix match (`doi:10.5072/STU/...` → STU
           catalog) via setting
           `EVILFLOWERS_DATAVERSE_GLOBAL_ID_PREFIXES` (list of
           `{prefix, catalog_url_name}`).
        3. Env override `DATAVERSE_CATALOG_URL_NAME` (deployment-wide
           default).
        4. `Catalog.objects.order_by("id").first()` fallback with a
           logger warning.
    - Document precedence in `docs/dataverse/catalog-routing.md`.
    - Tests for each path of the mapping.
    - Add a unique constraint on `(catalog, identifiers ->>
      'dataverse_pid')` so two concurrent prepublish callbacks for the
      same `global_id` cannot race past `SELECT FOR UPDATE` to create
      duplicates.
- [ ] **D4** Symmetric Dataverse sync:
    - When upstream removes a file, delete the corresponding
      `Acquisition`. Default to "dry run, log only"; require explicit
      `EVILFLOWERS_DATAVERSE_SYNC_DELETE_REMOVED=1` to enable
      destructive sync.
    - Add a `dry_run` flag to `DataverseSyncService` for operator
      review before destructive runs.
    - Respect upstream `restricted` flag: map to
      `AcquisitionType.RESTRICTED_ACCESS` instead of hardcoded
      `OPEN_ACCESS`.
- [ ] **D5** Configuration cleanup:
    - Remove `EVILFLOWERS_TEXT_SERVICE_URL` and `TEXT_SERVICE_REDIS_URL`
      from `compose.yml:57-58` (dead).
    - `dataverse/scripts/bootstrap-workflow.sh:33-35` — use
      `${DV_URL:-http://dataverse:8080}` instead of hardcoded.
    - `dataverse/compose.override.yml:2` — change the override target
      from `postgres` to `db` (the actual service name).
    - Add `docs/dataverse/` (new) with: integration overview,
      prepublish contract, workflow resume semantics, multi-tenant
      routing (per Q3 resolution), search-service flow, troubleshooting.
    - (`DV_PUBLIC_INTERNAL` → `DV_BASE_INTERNAL` rename already landed
      in IP-007 D9; `.env.example` may still need a touch-up if it
      diverged.)

### Phase 5: Polymorphic Storage Backend on `Acquisition`

- [ ] **E1** — Add `Acquisition.storage_backend = models.TextField(
  choices=[("local","local"),("external_url","external_url")],
  default="local")`. Migration backfills `external_url` for any row with
  `file_url IS NOT NULL`.
- [ ] **E1** — Add `apps/files/services.py::AcquisitionStorageService`
  with methods `open(acquisition) -> File`, `url(acquisition, request)
  -> str`, `exists(acquisition) -> bool`, `delete(acquisition) -> None`.
  Dispatches on `storage_backend`.
- [ ] **E1** — Replace ad-hoc `file_url` branches across
  `apps/files/views.py`, `apps/opds/schema.py`,
  `apps/opds2/services/manifest_builder.py`,
  `apps/readium/services/license_service.py` with calls to the service.
- [ ] **E1** — In `AcquisitionDownload.get`
  (`apps/files/views.py:63-68`), perform `check_entry_read` /
  `_check_ip_block` / UserAcquisition creation BEFORE the
  service-dispatched URL fetch. The bypass-on-redirect is closed by
  centralising the dispatch.
- [ ] **E1** — Dataverse sync (`DataverseSyncService`) sets
  `storage_backend="external_url"` and `file_url=...` on every
  Dataverse acquisition.
- [ ] **E1 lazy hashing** — `apps/core/models/acquisition.py:74-87` —
  make `base64` and `checksum` lazy: compute on demand, cache on the
  row (`checksum_cached: TextField | null`). Remove from
  `EntrySerializer.Detailed` default; expose via an explicit
  `?include=checksum,base64` query param.

### Phase 6: Search-Service Integration + Wiring Tightening

- [ ] **F1** — Add `apps/api/services/search_service_client.py::
  SearchServiceClient` with synchronous HTTP wrappers around `POST
  /search/elasticsearch` and `POST /search/semantic`. Read base URL
  from `SEARCH_SERVICE_URL`. Timeout 10s, no retry (operator can retry
  the search).
- [ ] **F1** — Extend `apps/opds/services/entry_search.py::
  EntrySearchService` (already shipped in IP-007 as the shared OPDS
  1.2 + OPDS 2.0 catalog-DB search) with a `mode`-aware dispatch:

    ```python
    @staticmethod
    def search(catalog, request, *, mode: SearchMode = SearchMode.CATALOG) -> QuerySet[Entry]:
        if mode is SearchMode.CATALOG:
            # existing behavior — DB-side EntryFilter
            ...
        elif mode is SearchMode.KEYWORD:
            doc_ids = SearchServiceClient().keyword(catalog_acquisition_ids(catalog), request.GET["query"])
            return Entry.objects.filter(catalog=catalog, acquisitions__pk__in=doc_ids).distinct()
        elif mode is SearchMode.SEMANTIC:
            doc_ids = SearchServiceClient().semantic(catalog_acquisition_ids(catalog), request.GET["query"])
            return Entry.objects.filter(catalog=catalog, acquisitions__pk__in=doc_ids).distinct()
    ```

  Update `apps/opds2/views/search.py` to read `mode` via
  `request.GET.get("mode", "catalog")` and pass through (default per
  Q5 resolution). OPDS 1.2 `SearchView` (already shipped in IP-007)
  keeps `mode=catalog` only — the mode parameter is OPDS-2-only.
- [ ] **F1** — Catalog scoping: the search service has no concept of
  catalog. Compute the `catalog_acquisition_ids(catalog)` allow-list
  (UUIDs of `Acquisition` rows whose `entry.catalog == catalog` AND
  pass the requester's ACL), pass it in the search-service request,
  and filter results post-hoc as a defensive second check.
- [ ] **F1** — Surface search-service-down: a 502 with `Retry-After`
  header when the search service times out, not a 500.
- [ ] **F1** — Document each mode via `docs/opds2/search.md` per Q5
  resolution (examples, failure semantics, OpenSearch descriptor
  template update advertising the `mode` parameter through the
  existing `apps/opds/schema.py::OpenSearchDescription`).
- [ ] **F1** — Add `apps/opds2/tests/test_search_modes.py` asserting:
  - `mode=catalog` matches today's behavior.
  - `mode=keyword|semantic` returns only entries from the requested
    catalog (cross-tenant leakage test).
  - Search-service down returns 502 + `Retry-After`, not 500.
- [ ] **F2** — Verify the search-service container's actual listen
  port; pin `compose.yml:147, 164-165` accordingly. Set
  `SEARCH_SERVICE_URL` to use the container-internal port; map host as
  `8001:{container}`. Default assumption per
  `docs/search_service_reference.pdf`: container `8000`, host `8001`.
- [ ] **F2** — `apps/dataverse/services/text_publish.py` (renamed from
  `text_service_client.py`):
    - Use Celery `app.send_task` with explicit `queue=
      "evilflowers_text_worker"`, `kwargs={...}`,
      `task_id=f"text:index:{acquisition.pk}"` for idempotency.
    - Set a `priority` field if the text-service worker honors it.
- [ ] **F2** — Add a new `apps/dataverse/management/commands/
  reindex_acquisitions.py` for operator-driven re-indexing.
- [ ] **F2** — Surface text-service-down: in `DataverseSyncService`,
  if `text-service` is unreachable for >60s, write a row to a new
  `IndexingFailure` model (or `NotificationLog`) so SREs can act.

## Technical Details

### Data Model Changes

```python
# apps/core/models/acquisition.py
class Acquisition(models.Model):
    # ... existing fields ...
    storage_backend = models.TextField(
        choices=[("local","local"),("external_url","external_url")],
        default="local",
    )
    checksum_cached = models.TextField(null=True, blank=True)
    # file_url remains; storage_backend says how it should be used.

    class Meta:
        constraints = [
            # Dataverse-PID uniqueness within a catalog
            models.UniqueConstraint(
                expressions=[
                    F("catalog"),
                    KeyTextTransform("dataverse_pid", "identifiers"),
                ],
                name="unique_dataverse_pid_per_catalog",
                condition=Q(identifiers__has_key="dataverse_pid"),
            ),
        ]
```

The `License` partial-`UniqueConstraint` on `(entry, user)` for
non-terminal states already exists in `develop` (IP-007 D4, migration
`apps/readium/migrations/0006_alter_license_unique_together_and_more.py`);
Phase 1 A1 of this IP layers application-level
`transaction.atomic()` + `Entry.select_for_update()` on top so the
borrow race is closed both at the application and database layer.

### API Changes

- License renewal endpoint accepts exact datetime in `requested_end`
  (already does at API level; the change is internal — stop rounding to
  days).
- License download response sets `Content-Type: application/pdf+lcp`
  for PDF-LCP content and `Cache-Control: private, no-store`.
- OPDS 1.2 acquisition feeds now emit
  `application/vnd.readium.lcp.license.v1.0+json` links for
  readium-enabled entries.
- `GET /opds/v2/{catalog}/search?query=X&mode=keyword|semantic|catalog`
  — new `mode` parameter.
- `POST /api/v1/dataverse-prepublish` — response shape unchanged;
  internal extraction transparent.
- `DELETE /api/v1/dataverse-sync` — route removed (was dead).
- New management command `python manage.py reindex_acquisitions
  [--catalog NAME] [--since DATE]`.

### Configuration

```sh
# Dataverse
EVILFLOWERS_DATAVERSE_CATALOG_MAP='{"5":"stu-catalog","12":"comenius"}'  # JSON
EVILFLOWERS_DATAVERSE_GLOBAL_ID_PREFIXES='[{"prefix":"doi:10.5072/STU/","catalog":"stu-catalog"}]'
DATAVERSE_CATALOG_URL_NAME=                              # global default
EVILFLOWERS_DATAVERSE_SYNC_DELETE_REMOVED=0              # safe default
DV_BASE_INTERNAL=http://dataverse:8080
DV_PUBLIC_BASE=https://data.example.org
DV_API_TOKEN=
DATAVERSE_RESUME_WORKFLOW=1
DATAVERSE_RESUME_WORKFLOW_ATTEMPTS=10
DATAVERSE_RESUME_WORKFLOW_INITIAL_DELAY=1

# Search service
SEARCH_SERVICE_URL=http://search-service:8000           # verified port
SEARCH_SERVICE_TIMEOUT_SECONDS=10

# Readium
EVILFLOWERS_READIUM_LSDSV_INTERNAL_HOST=lsdserver:8990
```

## Alternatives Considered

### Alternative 1: Postgres advisory lock keyed by `entry_id` for borrow serialization (A1)

**Pros**: Doesn't take a row-level lock; one less round-trip.

**Cons**: Locks live outside the ORM; debugging is harder;
cross-statement transaction semantics around advisory locks have edge
cases.

**Why not chosen**: `select_for_update` is standard Django; tests are
easier.

### Alternative 2: Leave the Dataverse god view in `apps/api`

**Pros**: No extraction work.

**Cons**: Already 823 lines; future Dataverse features keep growing it.
No tests. Mixes B2B integration with the CRUD API.

**Why not chosen**: Extract is the convention for B2B integrations;
matches what `apps/readium/` is.

### Alternative 3: Store Dataverse content locally (fetch + cache)

**Pros**: Single storage path; auth/perm checks trivially correct.

**Cons**: Duplicates storage between Dataverse and Catalog; loses
upstream metadata sync.

**Why not chosen**: The `file_url` model is the intentional design;
the polymorphic storage backend captures it cleanly.

### Alternative 4: Defer OPDS 1.2 LCP link parity until OPDS 1.2 is deprecated

**Pros**: No churn in 1.2 surface.

**Cons**: STU readers consume OPDS 1.2 today; Thorium can't import LCP
titles from 1.2 personal feeds. Two-line addition in
`AcquisitionEntry.from_model`.

**Why not chosen**: Cheap and high-impact.

## Trade-offs and Risks

### Trade-offs

- **`select_for_update` on the `Entry` adds latency** under contention.
  Acceptable for the borrow flow (rare under STU load).
- **Status Server PATCH on return/renew (A4) adds a round-trip** per
  state change. Required by spec.
- **Deferring `license_created` notification (C2) means the email
  arrives ~100ms later** than the API response. Imperceptible.
- **Extracting to `apps/dataverse/`** is a one-time move + tests cost.
  Pays off on every future Dataverse feature.
- **Celery-based workflow resume adds 50-200ms latency** vs the
  in-process thread. Acceptable; the resume is async by design.
- **Polymorphic storage backend forces touching every consumer of
  `Acquisition.file_url`** (~10 sites; each small).
- **Search-service integration adds a network dependency for OPDS
  `mode=keyword|semantic` searches.** Default `mode=catalog` preserves
  the no-network path.
- **OPDS 1.2 LCP link emission is feature-positive for clients** that
  exist; no client opt-out needed.

### Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| `select_for_update` causes deadlocks under heavy concurrent borrows of multiple entries | Medium | Acquire locks in consistent order (by entry.pk); tests for deadlock detection. |
| Status Server PATCH integration tests require a running LSD | Medium | Mock the Status Server in unit tests; run E2E against the dev LSD in CI. |
| `application/pdf+lcp` MIME breaks existing reader apps that expect `application/pdf` | Low | Verify against Thorium + STU reader; LCP profile §3 mandates the new MIME. |
| C4 passphrase-hash data migration fails on a row with non-hex content | Low | Filter to `length(hash) = 64 AND hash ~ '^[0-9a-fA-F]+$'`. |
| D2 event-based encryption fanout regresses to "no encryption" if event broker is misconfigured | High | Hard-fail at `Entry.save()` rather than swallow; documented in the new listener. |
| Extraction breaks an undocumented external caller of `/api/v1/dataverse-prepublish` | Low | URL unchanged for one release; deprecation warning. |
| Celery worker queue full → workflow resume delayed | Medium | Use a dedicated `dataverse` Celery queue with its own worker. |
| Catalog routing rolls out → existing Dataverse-imported entries appear "wrong catalog" after rule changes | High | One-time data migration to assign each existing entry to the correct catalog per the new rules. |
| Polymorphic storage backend migration on a large `Acquisition` table is slow | Medium | Migration in two steps: column add with default; later, backfill non-default rows. |
| Symmetric upstream-file deletion silently removes acquisitions operators didn't intend to drop | High | `EVILFLOWERS_DATAVERSE_SYNC_DELETE_REMOVED=0` by default. |
| `dataverse_pid` unique constraint discovers existing duplicates on deploy | Medium | Pre-migration audit query; manual cleanup. |
| Search service results include private-catalog entries from other users | High | Catalog scoping at the result-filter layer. Test specifically covers cross-tenant leakage. |
| Search service down → OPDS search times out | Medium | 10s timeout + 502 + `Retry-After`; `mode=catalog` always works. |

## Success Criteria

- [ ] Two concurrent borrow requests on a `readium_amount=1` entry
  produce one 201 and one 409 (no over-issue).
- [ ] A return PATCH at the License Gateway causes the next `GET
  /readium/v1/licenses/{id}/status` from the Status Server to return
  `status: returned` within 1 second.
- [ ] Downloading encrypted PDF content returns `Content-Type:
  application/pdf+lcp` and `Cache-Control: private, no-store`.
- [ ] `apps/readium/views/status_proxy.py::_rewrite_links` correctly
  rewrites all internal-host links (test fixture includes `status`,
  `publication`, `self`).
- [ ] `license_created` notification fires only after LCP issuance
  succeeds; on issuance failure, no notification row is created.
- [ ] `LCPServerClient.generate_license` persists the License row
  exactly once (after a successful LCP-server POST).
- [ ] LDAP-migrated user with a legacy lowercase `lcp_passphrase_hash`
  can open `.lcpl` and refresh it.
- [ ] `LicenseChecker` resides under `apps.readium.checkers`;
  `apps.core` has no `import apps.readium` anywhere.
- [ ] `_maybe_promote_next` no longer swallows arbitrary exceptions;
  metrics emitted on every catch.
- [ ] Re-encrypt with `force=True` deletes both the `EncryptedContent`
  row and the underlying file.
- [ ] `apps/dataverse/` exists with views, services, tests, and a
  passing pytest run.
- [ ] `DataverseSyncService.sync(payload)` is unit-testable in
  isolation against mocked `DataverseClient`.
- [ ] A pre-publish callback for a `global_id` that matches the
  explicit map routes to the configured catalog, not catalog #1.
- [ ] A worker restart during the workflow-resume window does not lose
  the resume — the Celery task completes after the worker reboots.
- [ ] Two concurrent pre-publish callbacks for the same `global_id`
  produce exactly one `Entry` (unique constraint + `SELECT FOR
  UPDATE`).
- [ ] `Acquisition.storage_backend = "external_url"` rows go through
  `AcquisitionStorageService.url()` for downloads.
- [ ] Upstream-deleted files: when run with
  `EVILFLOWERS_DATAVERSE_SYNC_DELETE_REMOVED=1`, the corresponding
  Acquisition is removed; otherwise logged as drift.
- [ ] `restricted` upstream files are imported as
  `AcquisitionType.RESTRICTED_ACCESS`.
- [ ] OPDS 1.2 acquisition feed for a readium-enabled entry includes
  the LCP license link
  (`application/vnd.readium.lcp.license.v1.0+json`).
- [ ] `GET /opds/v2/{c}/search?query=X&mode=keyword` returns results
  filtered to catalog `c` only.
- [ ] Search service down → 502 with `Retry-After`, not 500.
- [ ] `EVILFLOWERS_TEXT_SERVICE_URL` and `TEXT_SERVICE_REDIS_URL`
  removed from compose.
- [ ] LCP testing tools
  (`https://github.com/edrlab/lcp-testing-tools`) pass against staging.

## Future Considerations

- Per-catalog Status Server segregation (multi-tenant LSD) for future
  Slovak universities.
- LCP for Audio + LCP for Comics (extending `AcquisitionMIME`).
- A management command `python manage.py audit_license_state` that
  compares local `License.state` against the canonical Status Server
  state and reports drift.
- Bi-directional sync: catalog → Dataverse (currently only Dataverse
  → catalog).
- Streaming PDF extraction (don't fetch the full PDF to the
  text-service worker; stream).
- A "preview chunk" in OPDS 2.0 search results (the search service
  returns text chunks; OPDS could surface them).
- Dataverse OAI-PMH ingest as an alternative to webhook-driven sync.

## References

- `apps/readium/services/license_service.py:202-313` — non-atomic
  create (A1).
- `apps/readium/services/reservation_service.py:148-217` — promote_next
  + expire_unclaimed lock scope (A2, A3).
- `apps/readium/services/status_server_client.py:92-145` — missing
  PATCH on return/renew (A4).
- `apps/readium/services/content_encryption_service.py:89-94` —
  premature REGISTERED (A5).
- `apps/readium/views/content.py:62-66` — MIME + cache header (B1).
- `apps/readium/views/status_proxy.py:38` — link rewrite (B2).
- `apps/opds/schema.py::AcquisitionEntry.from_model` — OPDS 1.2 LCP
  link emission (B5).
- `apps/core/checkers.py:11` — directional inversion (cleanup in
  Phase 3).
- `apps/api/views/dataverse.py:202-214` — daemon-thread workflow
  resume (D2).
- `apps/api/views/dataverse.py:246-823` — god view (D1).
- `apps/api/views/dataverse.py:367-389` — catalog routing comment vs
  implementation (D3).
- `apps/api/services/text_service_client.py` — stateless class shape
  (D1, F2).
- `apps/core/models/acquisition.py` — `file_url` field, `base64` /
  `checksum` properties (E1).
- `apps/opds2/views/search.py` — uses `EntrySearchService` (catalog
  mode); needs `mode=keyword|semantic` extension (F1).
- `apps/opds/services/entry_search.py::EntrySearchService` — shared
  catalog-DB search layer shipped in IP-007; Phase 6 F1 extends with
  search-service modes.
- `docs/search_service_reference.pdf` — search service API contract.
- `compose.yml:57, 147, 164-165` — dead config + search-service port
  (D5, F2). DV env mismatch fixed in IP-007 D9.
- [IP-003: LCP EDRLab Certification Readiness](ip-003-lcp-edrlab-certification.md) —
  this IP fills the gaps the audit found post-IP-003.
- [IP-004: Per-Entry Active-License Limits](ip-004-readium-amount-configurability.md) —
  Phase 1 of this IP makes IP-004's contract hold under concurrency.
- [IP-007: Crash-on-First-Contact Bug Triage](ip-007-crash-on-first-contact-triage.md) —
  ✅ Implemented in commit `1953da4`. Established the baseline this
  proposal builds on: partial `UniqueConstraint`, signal name-mangling
  fix, `Entry.first_author_name`, `DetailType.FORBIDDEN`,
  `EntrySearchService`, `OpenSearchDescription`, `parse_int_query`,
  `DV_BASE_INTERNAL`, single text-service publish, OPDS 1.2 root /
  Latest fixes, `license_renewed` notification, complete
  `NotificationType` enum.
- [IP-010: Multi-Tenancy & ACL Correctness](ip-010-multi-tenancy-acl-correctness.md) —
  shares the `file_url` redirect security fix (M2 region) at the
  storage-service dispatch point.
- READIUM_ACTION_PLAN.md §7 — OPDS LCP link requirement.

## Review Questions

**Status**: ✅ Resolved
**Review Date**: 2026-05-25
**Resolution Date**: 2026-05-25
**Reviewer**: Claude AI

---

### Q1 ⚠️: Status proxy mirroring (C6) — proxy-only or proxy-and-mirror?

**Issue**: Today `ReturnProxyView` proxies the upstream Status Server
PUT then locally writes to the License row via
`StatusServerClient.return_license`. Two writes, two sources of truth.

**Question**: Proxy-only and refetch from LSD, or proxy + mirror
locally?

**Options**:

- [X] **A**: Proxy + refetch (Recommended). LSD is the canonical state;
  local license state is a cache. Read-after-write from LSD on every
  state change.
- [ ] **B**: Proxy + write locally. Two writes; reconcile on read.
- [ ] **C**: Local write only + async LSD sync via Celery. Decouples but
  adds eventual-consistency complications.

**Answer**:

```
Alway properly sync. Chect the whole lifecycle.
```

**Resolution**:

```
Option A chosen — LSD is the canonical state; local License.state is a
cache. Phase 1 A4 + Phase 3 C6 will be expanded into a full
license-lifecycle sync audit covering every transition that produces a
state delta in LSD:

  - return     (ReturnProxyView          → PATCH /licenses/{id}/status returned)
  - renew      (RenewProxyView           → PATCH /licenses/{id}/status active + new end_date)
  - revoke     (admin revoke action      → PATCH /licenses/{id}/status revoked)
  - cancel     (admin cancel READY       → PATCH /licenses/{id}/status cancelled)
  - register   (DeviceRegistrationProxy  → POST  /licenses/{id}/register → reflect device_count)
  - expire     (LSD natural expiry       → on next read, refetch + reconcile)

For every transition the catalog is involved in:
  1. PATCH the Status Server first.
  2. On success, GET the canonical status document.
  3. Reconcile `License.state`, `License.device_count`, `License.ends_at`
     from the LSD response (local DB is a cache).
  4. On PATCH failure: surface 502 + Retry-After; do NOT write locally.

Add `apps/readium/services/status_server_sync.py::StatusServerSyncService`
as the single dispatch point so the audit is enforced by code shape
(every callsite goes through one service). Add tests for each transition
asserting the LSD PATCH is issued AND the local row reflects the
post-PATCH document, not the pre-PATCH guess.

Implementation Plan updated: A4 and C6 merge into a single subsection
"License lifecycle ⇄ LSD sync" listing all six transitions above.
```

---

### Q2 🔴: Upstream-file deletion (Phase 4 D4) — destructive by default, dry-run by default, or never?

**Issue**: Dataverse can remove a file from a dataset between publish
revisions. Today the catalog never deletes the corresponding
Acquisition. The architecturally correct behavior is symmetric: file
removed upstream ⇒ acquisition removed downstream.

**Question**: How aggressive should the symmetric-delete behavior be?

**Options**:

- [X] **A**: Dry-run by default, opt-in destructive via
  `EVILFLOWERS_DATAVERSE_SYNC_DELETE_REMOVED=1` (Recommended). Safe by
  default; operators see drift in logs.
- [ ] **B**: Destructive by default. Matches Dataverse intent.
- [ ] **C**: Never delete; just mark `Acquisition.is_orphaned=true`
  (new field).

**Answer**:

```
Log properly, later will be event or alert.
```

**Resolution**:

```
Option A chosen — dry-run by default; destructive sync opt-in via
`EVILFLOWERS_DATAVERSE_SYNC_DELETE_REMOVED=1`. Logging discipline matters
more than the default flag because the alerting / event story comes
later.

Phase 4 D4 will be tightened on logging:
  - When the sync detects an upstream-removed file and
    `EVILFLOWERS_DATAVERSE_SYNC_DELETE_REMOVED=0`, log at WARNING with
    structured fields: `event=dataverse.upstream_file_removed`,
    `acquisition_id`, `entry_id`, `catalog_url_name`, `global_id`,
    `dataverse_pid`, `upstream_revision`. These are the fields a future
    alert or Loki query needs.
  - Emit one `INFO` log per sync run summarising counts:
    `event=dataverse.sync_summary upserts=N updates=N drifted=N
    would_delete=N actually_deleted=N`.
  - Emit a `dataverse.upstream_file_removed` domain event via
    `apps/events/` (per Q6 resolution). Even with no consumer wired
    today, the event makes the future alert pipeline a configuration
    change rather than a code change.
  - Add a management command `python manage.py dataverse_drift
    [--catalog NAME]` that prints the drift report on demand for
    operator review before flipping the destructive flag.

No change to the default flag; behavior remains safe-by-default.
```

---

### Q3 ⚠️: Catalog routing precedence (Phase 4 D3) — strict or fall-through?

**Issue**: The routing precedence is explicit map → prefix → env
default → first catalog. Question is whether a miss at the
explicit-map level should *fail loudly* (no catalog found, reject the
prepublish callback) or *fall through* to the env default.

**Question**: Strict or fall-through?

**Options**:

- [X] **A**: Fall-through with a logger warning at each step
  (Recommended). Smooth rollout; operators see warnings and add map
  entries.
- [ ] **B**: Strict: if explicit map exists at all, require a hit;
  otherwise 422.
- [ ] **C**: Configurable via
  `EVILFLOWERS_DATAVERSE_ROUTING_MODE=strict|fallthrough`.

**Answer**:

```
Explain and document properly.
```

**Resolution**:

```
Option A chosen — fall-through with logger warnings. Resolution focuses
on making the precedence legible at runtime and in docs:

1. `CatalogRouter.resolve(payload) -> RoutingDecision` returns not only
   the catalog but also a `matched_rule: {"explicit_map", "global_prefix",
   "env_default", "fallback_first_catalog"}` enum value and the input
   that triggered it (e.g., the matched prefix or dataset_id). The
   prepublish handler logs at INFO on `explicit_map`/`global_prefix`, at
   WARNING on `env_default`, and at ERROR on `fallback_first_catalog`
   with the payload's `global_id` for grepability.
2. `docs/dataverse/catalog-routing.md` (new) documents the precedence
   with a worked example per step:

   - **Step 1 — Explicit map**:
     `EVILFLOWERS_DATAVERSE_CATALOG_MAP='{"5":"stu","12":"comenius"}'`
     → dataset_id=5 routes to `stu`.
   - **Step 2 — Global ID prefix**:
     `EVILFLOWERS_DATAVERSE_GLOBAL_ID_PREFIXES=
     '[{"prefix":"doi:10.5072/STU/","catalog":"stu"}]'`
     → `global_id=doi:10.5072/STU/ABC123` routes to `stu`.
   - **Step 3 — Env default**:
     `DATAVERSE_CATALOG_URL_NAME=stu` catches everything not matched
     above. WARNING logged.
   - **Step 4 — Fallback first catalog**: only if everything else
     misses. ERROR logged with `global_id` so ops can patch the map.
3. The doc includes a "How to add a new tenant" runbook (5-step
   checklist) and a "How to debug a wrongly-routed dataset" runbook
   (grep the structured logs by `matched_rule`/`global_id`).
4. `apps/dataverse/tests/test_router.py` includes a parametrized test
   asserting the matched_rule for each precedence step.
5. Operator-facing: a new `python manage.py dataverse_route
   --global-id=... [--dataset-id=...]` command prints the routing
   decision without performing a publish, so operators can verify the
   map before flipping the live config.

No change to default precedence; behavior remains fall-through.
```

---

### Q4 ⚠️: OPDS 1.2 borrow link target (B5) — cross-link to OPDS 2.0 or implement OPDS 1.2 borrow

**Issue**: Phase 2 wants OPDS 1.2 to emit a borrow link. Two options:
point at the existing OPDS 2.0 borrow endpoint (clients follow
cross-version links), or build a new OPDS 1.2 borrow view.

**Question**: Cross-link or new endpoint?

**Options**:

- [X] **A**: Cross-link at the OPDS 2.0 borrow endpoint (Recommended).
  Less code; works for any client that follows links.
- [ ] **B**: Build OPDS 1.2 borrow view, mirroring the OPDS 2.0 one.
- [ ] **C**: Carry a direct `.lcpl` link only when the user already has
  an active license; never advertise a "borrow now" path. Matches what
  STU readers do today.

**Answer**:

```
Properly refactor for nice DRY, Introfuce some service layer
```

**Resolution**:

```
Option A confirmed (cross-link to OPDS 2.0 borrow endpoint), with the
DRY refactor promoted from "small helper" to a real service layer:

1. Replace the inline `attach_lcp_license_link` helper sketch with a
   service: `apps/opds/services/borrow_link.py::BorrowLinkResolver`
   (or `apps/readium/services/borrow_link.py` if it stays
   readium-flavoured — pick whichever side owns the domain model;
   recommendation: opds-side because the *link emission* is an OPDS
   concern even though the *protected content* is readium).

   ```python
   class BorrowLinkResolver:
       def __init__(self, request, *, opds_version: Literal["1.2","2.0"]):
           ...
       def borrow_url(self, entry: Entry) -> str | None: ...
       def license_url(self, license: License) -> str: ...
       def emit_links(self, entry: Entry, active_license: License | None) -> list[Link]: ...
   ```

   `emit_links` returns:
     - `rel=http://opds-spec.org/acquisition/borrow`,
       `type=application/vnd.readium.lcp.license.v1.0+json` pointing at
       the OPDS 2.0 borrow endpoint (cross-version link).
     - if `active_license is not None`:
       `rel=http://opds-spec.org/acquisition`,
       `type=application/vnd.readium.lcp.license.v1.0+json`
       pointing at `/readium/v1/licenses/{id}.lcpl`.
     - the direct download link is suppressed by the resolver for
       readium-enabled entries.

2. Single point of dispatch:
   - `apps/opds2/views/borrow.py:70-78` and
     `apps/opds2/services/feed_builder.py:193-201` (today's duplicated
     OPDS 2.0 emit) replaced with `resolver.emit_links(entry, license)`.
   - `apps/opds/schema.py::AcquisitionEntry.from_model` (OPDS 1.2)
     consumes the same resolver, formatting each `Link` as the
     OPDS-1.2 `<link>` element (vs OPDS-2 JSON link object).
   - Format conversion stays local to each schema; the link *semantics*
     (rel, type, target URL) live in one place.

3. Tests assert byte-for-byte parity between OPDS 1.2 and OPDS 2.0 for
   the LCP-link tuple `(rel, type, target_url)` on the same entry.

4. The `attach_lcp_license_link` mention in Phase 2 B5 and Phase 1
   "shared abstractions" is updated to point at `BorrowLinkResolver`.

5. Future: a third channel (e.g., a JSON-feed API for SPA) can consume
   the same resolver — proves the abstraction.

Implementation Plan updated: Phase 2 B5 references the
`BorrowLinkResolver` service; Phase 3 D4 (deduplication of
`_get_license`) and the borrow-link dedup share the same "promote
service over helper" pattern.
```

---

### Q5 ⚠️: OPDS 2.0 search `mode` default (Phase 6)

**Issue**: Phase 6 adds `?mode=catalog|keyword|semantic` with a
default. The choice between defaults shapes the UX: `catalog` (today's
behavior, no network call), `keyword` (richer results, search-service
dependency), or `auto` (try semantic, fall back).

**Question**: What is the default `mode`?

**Options**:

- [X] **A**: `mode=catalog` (Recommended). Preserves today's behavior;
  opt-in to search service.
- [ ] **B**: `mode=keyword`. Better UX; introduces a hard dependency on
  the search service for OPDS search.
- [ ] **C**: `mode=auto` — try semantic first, fall back to catalog on
  timeout. Most magic, hardest to debug.

**Answer**:

```
Document properly.
```

**Resolution**:

```
Option A confirmed — `mode=catalog` default. Documentation work is the
resolution scope:

1. `docs/opds2/search.md` (new) covers each mode explicitly:

   - **`mode=catalog`** (default):
     - DB-side `EntryFilter` over `title`, `authors__name`, `categories__term`.
     - No network call to the search service.
     - Catalog-scoped via the standard catalog ACL filter.
     - Best for: navigation, browsing, fast results.
     - Example: `GET /opds/v2/stu/search?query=biology`

   - **`mode=keyword`**:
     - Calls search-service `POST /search/elasticsearch` with the
       catalog's `document_id` allow-list (built from
       `Acquisition.uuid` for accessible entries).
     - Returns matches inside the indexed PDF text content + metadata.
     - Best for: looking inside the document.
     - Example: `GET /opds/v2/stu/search?query=mitochondria&mode=keyword`

   - **`mode=semantic`**:
     - Calls search-service `POST /search/semantic` with the same
       allow-list.
     - Vector / embedding search.
     - Best for: natural-language queries, "books about X".
     - Example:
       `GET /opds/v2/stu/search?query=research+on+cell+structure&mode=semantic`

2. Document the failure mode: search-service unreachable or 5xx ⇒ 502
   with `Retry-After: 10`. Clients can retry or downgrade to
   `mode=catalog` themselves.

3. Document catalog scoping: the search service has no notion of
   catalogs; results are filtered after-the-fact via the
   acquisition→entry→catalog mapping. Tests cover the cross-tenant
   leakage case.

4. Add an OpenSearch descriptor that advertises the `mode` parameter
   via a templated URI: `/opds/v2/{catalog}/search?query={searchTerms}
   &mode={searchMode?}` so OPDS clients can discover the option.

5. Add `python manage.py search_check --catalog NAME --query X --mode
   keyword` for operators to probe the search service from the host
   (skips OPDS rendering; surfaces raw search-service response).

No change to default mode; behavior preserves today's path.
```

---

### Q6 ℹ️: D2 (entry → encryption fanout) — domain event or direct Celery task?

**Issue**: The cross-app coupling from `apps.core.models.entry.Entry.
save` to `apps.readium.services.ContentEncryptionService` is
synchronous today. Two cleanups: (a) publish a domain event
`entry.changed` and have `apps.readium` subscribe; (b) directly enqueue
a Celery task from a readium-side post_save signal that doesn't import
readium internals from core.

**Question**: Domain event or direct Celery dispatch?

**Options**:

- [X] **A**: Domain event via `apps/events/` (Recommended). Aligns with
  the existing event broker; testable in isolation.
- [ ] **B**: Direct Celery dispatch from a readium-side signal handler.
- [ ] **C**: Keep synchronous, just narrow the import surface.

**Answer**:

```
Check for other candidates for such migration.
```

**Resolution**:

```
Option A chosen — domain event via `apps/events/`. Expanded scope: audit
the codebase for other cross-app `post_save` / module-load couplings
that should follow the same migration. Initial candidates identified
from the May 2026 audit:

| Today's coupling | Domain event proposed | Subscriber |
|---|---|---|
| `apps/core/models/entry.py:138-157` `Entry.save()` → `ContentEncryptionService` (sync) | `entry.changed` | `apps/readium/listeners.py::on_entry_changed` enqueues `encrypt_acquisition` |
| `apps/notifications/signals.py:5` imports `apps.readium.models.License` at module load + `post_save` on `License` | `license.created`, `license.returned`, `license.revoked`, `license.renewed` | `apps/notifications/listeners.py::on_license_event` enqueues `send_notification` |
| `apps/readium/signals.py` `License.post_save` → `send_notification.delay` direct | same events as above | same listener |
| `apps/readium/tasks.py:75` license-expiring-soon notification fired SYNC from a beat task | `license.expiring_soon` | notification listener (uniform path) |
| `apps/api/views/entries.py:269-289` PDF upload → `TextServiceClient.process_acquisition.delay` direct | `acquisition.uploaded` (after IP-007 D10 dedup) | `apps/dataverse/listeners.py::on_acquisition_uploaded` enqueues text-service publish |
| `apps/dataverse/services/sync.py` upstream-removed file (Q2 resolution) | `dataverse.upstream_file_removed` | future alert pipeline (no consumer today; event is the seam) |

Scope decision for THIS proposal:
  - In-scope: `entry.changed` (D2) — already in Phase 3.
  - In-scope: `license.*` and `acquisition.uploaded` since they touch
    the same files Phase 3/Phase 4/Phase 5 already edits.
  - The audit table above lands in `docs/events/migration-candidates.md`
    so future IPs (or a dedicated "events refactor" IP) can pick up the
    rest.

Per-candidate work in this proposal:
  1. `apps/events/` gains explicit registration for `entry.changed`,
     `license.created`, `license.returned`, `license.revoked`,
     `license.renewed`, `license.expiring_soon`,
     `acquisition.uploaded`, `dataverse.upstream_file_removed`.
  2. A new `apps/readium/listeners.py` and
     `apps/notifications/listeners.py` subscribe and replace direct
     `delay()` calls. The `apps/notifications/signals.py:5` module-load
     import of `apps.readium.models.License` is removed; the listener
     receives the event payload, not a Django Model.
  3. Keep the direct `send_notification.delay()` callsites as a
     deprecation period (warn-once log) for one release; remove after
     the listener proves stable in staging.
  4. Phase 3 D2 (cleanup) text updated to reference the audit table.

Implementation Plan: Phase 3 D2 expanded into the candidates table;
Phase 6 (search-service) adds the `acquisition.uploaded` event emit at
the IP-007 D10 dedup point.
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-05-14 | Claude AI | Initial draft IP-008 based on May 2026 readium audit (post-IP-003, post-IP-004). |
| 2026-05-14 | Claude AI | Initial draft IP-009 based on May 2026 dataverse + text-service audit. |
| 2026-05-25 | jdubec | Consolidated IP-008 (Readium) + IP-009 (Dataverse) into a single executable proposal. Folded in OPDS 1.2 LCP link parity (from former IP-011 Cluster C) and OPDS 2.0 search-service integration (from former IP-011 Cluster F). Dropped cross-references to former IP-005 (security hardening) and IP-006 (deployability) — those concerns are deferred to a future hardening cycle. Focus is functional state for the May→August window. |
| 2026-05-25 | jdubec | Resolved Review Questions Q1–Q6. Q1 (LSD sync): expanded into a full license-lifecycle sync audit via `StatusServerSyncService` covering return/renew/revoke/cancel/register/expire. Q2 (upstream-file deletion): kept safe-default dry-run, added structured logging + `dataverse.upstream_file_removed` event emission + `dataverse_drift` management command. Q3 (routing precedence): kept fall-through, added `RoutingDecision.matched_rule`, `docs/dataverse/catalog-routing.md` with worked examples per step, `dataverse_route` debug command. Q4 (OPDS 1.2 borrow link): promoted `attach_lcp_license_link` from helper to `BorrowLinkResolver` service-layer abstraction shared across OPDS 1.2 / OPDS 2.0. Q5 (search mode default): kept `mode=catalog`, added `docs/opds2/search.md` documenting each mode + `search_check` management command. Q6 (event fanout): audited cross-app couplings; in-scope events `entry.changed`, `license.*`, `acquisition.uploaded`, `dataverse.upstream_file_removed` added with listeners; full migration-candidates table moved to `docs/events/migration-candidates.md`. Status flipped to ✅ Resolved. |
| 2026-05-25 | jdubec | Rebased on top of IP-007 (commit `1953da4`). Added a "Baseline assumed" section to Problem Statement listing what's already in `develop`: partial `UniqueConstraint` on License, signal name-mangling fix, `Entry.first_author_name`, `DetailType.FORBIDDEN`/`INTERNAL_ERROR`, `apps/opds/services/entry_search.py::EntrySearchService`, `OpenSearchDescription` + URL-encoded template, `apps/api/utils/parse.py::parse_int_query`, `DV_BASE_INTERNAL` rename, single text-service publish, OPDS 1.2 root/Latest fixes, `license_renewed` notification, complete `NotificationType` enum. Cluster D5 trimmed (DV env rename removed). Phase 4 D5 trimmed accordingly. Phase 2 B5 reworked to consume the existing OPDS 1.2 `SearchView` baseline and to thread through `BorrowLinkResolver`. Phase 6 F1 reworked to EXTEND `EntrySearchService` with a `mode` parameter rather than rewriting `apps/opds2/views/search.py` from scratch. References section updated with IP-007 commit hash and new shared-service citations. |
| 2026-05-25 | jdubec | **Implemented.** All six phases shipped: Phase 1 (concurrency A1–A6 + C1/C2/C6/C7); Phase 2 (LSD/RWPM compliance B1–B5 incl. `BorrowLinkResolver` shared service); Phase 3 (service correctness C3–C8 + D1 `LicenseChecker` move to `apps.readium` + D2 signal direction reversal + D3 OPDS 2 passphrase pre-check dedup + D4 `LicenseLookupMixin`); Phase 4 (full `apps/dataverse/` extraction — `DataverseClient`, `DataverseSyncService`, `CatalogRouter` with documented precedence, Celery `resume_workflow`, partial unique constraint on `(catalog, dataverse_pid)`, `RESTRICTED_ACCESS` mapping, drift detection); Phase 5 (`Acquisition.storage_backend` enum + `AcquisitionStorageService` dispatch + lazy `checksum_cached` + auth-before-redirect in `AcquisitionDownload`); Phase 6 (`SearchServiceClient` + `EntrySearchService.search(mode=)` + OPDS 2.0 `?mode=keyword/semantic` with 502+Retry-After + `reindex_acquisitions` command + `docs/opds2/search.md`). Migrations: `core/0036_uppercase_lcp_passphrase_hash`, `core/0037_acquisition_restricted_access`, `core/0038_dataverse_pid_unique`, `core/0039_acquisition_storage_backend`. Status flipped to ✅ Implemented. |
