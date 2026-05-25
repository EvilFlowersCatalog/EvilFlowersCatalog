---
draft: true
date: 2026-05-25
authors:
  - jdubec
categories:
  - Feature
tags:
  - readium
  - lcp
  - lifecycle
  - integration
  - elvira-portal
---

# IP-009: Readium Integration Closeout — Lifecycle Vocabulary, Expiry Reconcile, and Single-Use Download Tokens

IP-008 closed the structural Readium gaps (atomic borrow, LSD spec compliance, OPDS LCP link parity, search-service
integration, Dataverse extraction). A 2026-05-25 review with the elvira-portal team surfaced a remaining cluster of
*integration-surface* defects that block the frontend from completing the borrow → renew → return → re-borrow loop:
the renewal action gets rejected at form validation because the state enum has no `renewed` member, naturally expired
licenses are never transitioned to `EXPIRED` so re-borrowing the same title returns 400, the `StatusServerClient`
PATCHes the canonical LSD but never reconciles back from it, and `.lcpl` downloads ship the long-lived JWT in the
URL `?access_token=` parameter. IP-009 finishes the EFC side of the Readium integration: it separates lifecycle
*actions* from lifecycle *states*, reconciles license state with the LSD on both write and a periodic sweep,
introduces single-use download tokens, adds a `renewal_count` to the License model, and freezes a documented client
contract elvira-portal can implement against.

<!-- more -->

## Status

**Status**: Draft
**Last Updated**: 2026-05-25
**Implementation**: Not started — IP-008 prerequisite landed in commits
`c72d5c0`, `75b3b7d`, `4a98822`, `f3c132b`, `6dfab49`.

## Problem Statement

### Baseline assumed (IP-008 already landed)

This proposal builds on IP-008's `develop` head. The following are now in
`develop` and are NOT in scope here:

- `LicenseService.create_license` is atomic with `Entry.select_for_update()`
  (IP-008 A1); reservation promotion under correct lock scope (A2/A3);
  `StatusServerClient.return_license` / `renew_license` PATCH the LSD first
  before the local write (A4) — but they *guess* the post-PATCH state from
  the request body instead of refetching the canonical status doc (Q1
  resolution still outstanding; see C1 below).
- `LicenseRenewalsView` (`apps/readium/views/licenses.py:246-304`) accepts
  exact `requested_end` datetimes and forwards `decision.new_end` to
  `LicenseService.renew_license` without rounding (C7).
- `ReturnProxyView` (`apps/readium/views/status_proxy.py:143-197`) proxies
  the LSD `PUT /licenses/{id}/return`, mirrors locally inside a
  `transaction.atomic` + `select_for_update`, and best-effort updates LCP
  rights (C6 — double-mirror removed).
- `BorrowLinkResolver` shared by OPDS 1.2 / 2.0 (B5).
- `AcquisitionStorageService` for `storage_backend in (local, external_url)`
  (Phase 5).
- OPDS 2.0 `?mode=catalog|keyword|semantic` search dispatch (Phase 6).

### Cluster A — Lifecycle vocabulary mismatch

**A1. `UpdateLicenseForm.state` validates against the wrong enum.**
- `apps/readium/forms.py:14-16` —
  `state = forms.ChoiceField(choices=License.LicenseState.choices, required=False)`.
- `License.LicenseState` (`apps/readium/models.py:69-75`) enumerates
  *states the license can be in*: `ready / active / returned / expired /
  revoked / cancelled`. There is no `renewed` — renewal is a verb, not a
  resting state; a successful renewal leaves the license in `active`.
- `LicenseDetail._handle_state_change`
  (`apps/readium/views/licenses.py:161-221`) dispatches on
  `new_state == "renewed"` (and other verbs), but the form rejects it
  before the view ever sees it. Every renewal POST from elvira-portal
  fails with 400 "Select a valid choice."
- The elvira-portal TS enum (`src/utils/interfaces/licenses.ts`)
  compounds the issue: it has `draft / active / returned / expired /
  revoked / cancelled` — a typo (`draft` for `ready`) and no `renewed`
  either. Compile-time, the frontend cannot construct a renewal
  payload.

**A2. Two endpoints model renewal differently.**
- `PUT /readium/v1/licenses/{id}` with `{state: "renewed", duration: "P14D"}`
  routes through `LicenseService.renew_license(license, new_duration_days=N)`.
- `POST /readium/v1/licenses/{id}/renewals` with `{requested_end: <iso>}`
  routes through `evaluate_renew` policy then
  `LicenseService.renew_license(license, new_end_date=<dt>)` (post-IP-008).
- The Status Server's `renew_custom_url` callback uses the second
  endpoint (LSD §6.5). elvira-portal's renewal UI uses the first. The
  two paths diverge on payload shape, validation, and policy.

### Cluster B — Naturally expired licenses are never reconciled

**B1. `License.state` is never transitioned to `EXPIRED`.**
- `License.is_expired` (`apps/readium/models.py:103-107`) is a Python
  property — `timezone.now() > self.expires_at`. The DB column never
  changes when natural expiry passes.
- `LicenseService.can_user_borrow` (`apps/readium/services/license_service.py:170-181`)
  filters by `state__in=[READY, ACTIVE]` only. A loan whose
  `expires_at` lapsed yesterday but whose `state` is still `ACTIVE`
  triggers `"User already has an active license for this entry"` —
  exactly the error trace in the meeting notes.
- The expiring-soon Celery task (`apps/readium/tasks.py:33-80`) reminds
  the user; no companion task flips the state.

**B2. The unique constraint blocks the second borrow.**
- `License`'s partial `UniqueConstraint` (IP-007) ties uniqueness to
  `state__in=("ready","active")` — exactly the states that B1 never
  leaves. A user who had one expired-but-not-transitioned license
  cannot get a second one for the same entry.

**B3. The Status Server already knows.**
- The LSD response document includes the canonical status, computed
  from `rights.end`. The catalog can refetch and reconcile.

### Cluster C — LSD read-after-write reconcile not implemented

**C1. The `StatusServerClient` PATCHes then guesses.**
- `apps/readium/services/status_server_client.py:92-208`. After PATCHing
  the LSD, every method writes the *expected* local state
  (`LicenseState.RETURNED`, `LicenseState.REVOKED`, etc.) without
  re-reading the canonical document.
- IP-008 Q1 resolution explicitly said: "1. PATCH the Status Server
  first. 2. On success, GET the canonical status document. 3.
  Reconcile `License.state`, `License.device_count`, `License.ends_at`
  from the LSD response (local DB is a cache)." Step 2-3 are not
  implemented.
- Consequences:
  - If the LSD rejects the transition (e.g., already revoked, returned
    twice), the catalog writes a phantom local state.
  - `device_count` drift over time when LSD prunes inactive devices
    differently from the local mirror.

### Cluster D — Long-lived JWTs in `.lcpl` download URLs

**D1. `useDownloadLicense.tsx` concatenates the user's bearer JWT into
the URL.**
- elvira-portal `dev` (`src/hooks/api/licenses/useDownloadLicense.tsx`)
  produces both `…/readium/v1/licenses/{id}.lcpl?access_token=${auth.token}`
  and the same string under the `thorium://` scheme.
- The token is the user's regular access token — 30-minute idle
  expiry (post-`109fa42`), but every page-refresh re-reads the same
  cached token. Whenever the user has been idle, the URL is stale at
  click-time and the download fails.
- The token also appears in:
  - browser history (URL bar)
  - referrer header to `thorium://` handler
  - any reverse-proxy access log that records query strings
  - clipboard, if the user right-clicks → copy link
- The clipboard / history exposure is a privilege-escalation footgun:
  anyone with the URL has the user's full API surface for the token's
  lifetime.

**D2. `LoansTable.tsx` passes the wrong UUID.**
- `src/components/items/loans/LoansTable.tsx`:
  `const licenseId = item.lcp_license_id || item.id;` then calls
  `openInThorium(licenseId)`. The backend route is `/readium/v1/
  licenses/<uuid:license_id>.lcpl` and resolves `license_id` as the
  *local* primary key (`License.objects.get(pk=…)`). When
  `lcp_license_id` is populated (post-issuance) the URL 404s.
- Strictly an elvira-portal defect, but the contract should make it
  unambiguous: serializer exposes a single `download_url` field, the
  frontend should not concatenate UUIDs.

### Cluster E — Renewal counter / cap not modelled

**E1. The `License` row records no renewal history.**
- `License` has `created_at`, `updated_at`, `expires_at` — nothing
  named `renewal_count` or `renewals`. The `evaluate_renew` policy
  caps `requested_end` at `EVILFLOWERS_READIUM_MAX_RENEW_DAYS` *from
  now* but cannot enforce "at most N renewals per loan."
- STU policy is "loan can be extended twice." The catalog cannot
  enforce that limit, surface remaining renewals to the user, or audit
  who renewed what when.

### Cluster F — Integration contract for elvira-portal not documented

**F1. The license/queue fields already on `EntrySerializer` are invisible
to the frontend.**
- IP-007 + IP-008 shipped `lcp_state`, `available_slots`, `total_slots`,
  `active_count`, `over_saturated`, `next_available_at`,
  `user_active_license_id`, `queue_length`, `user_reservation_id`,
  `user_position` — populated via `lcp_state_mapping` in **both** list
  and detail responses (`apps/api/views/entries.py:117, 178, 227, 331`).
- elvira-portal's `ILicense` / `ILicenseEntry` TS types make no
  reference to any of them. The entry-detail UI builds availability
  state by calling `/readium/v1/entries/{id}/availability` instead of
  reading the inlined fields, doubling round-trips.
- There is no single page in `docs/` that says "here is the contract
  the frontend should use; here is which endpoint corresponds to which
  user action." Tomáš asked for one in the meeting.

**F2. HTTP method confusion on return.**
- The meeting captured "405 on PATCH return." Backend has only `PUT
  /readium/v1/licenses/{id}` (state machine) and `PUT
  /readium/v1/licenses/{id}/return` (LSD proxy). No PATCH. elvira-portal
  `dev` HEAD already uses PUT; older deployed builds may not, and the
  contract should call this out explicitly.

### Who is Affected

- **STU readers** — cannot renew (Cluster A), cannot re-borrow titles
  whose previous loan expired (Cluster B), get stale state for
  long-running loans (Cluster C).
- **Operations & security review** — long-lived JWTs in URLs (Cluster
  D) are a soft secret exposure.
- **Librarians** — cannot enforce the "two renewals max" policy
  (Cluster E), have no visibility into renewal history.
- **elvira-portal team (Tomáš)** — no shared client contract (Cluster
  F), every UI flow requires re-reading backend source.

### Consequences of Not Addressing

- The borrow flow is broken end-to-end for every user who has ever
  taken a loan: their first loan expires, their state stays `ACTIVE`,
  the unique constraint blocks the second loan forever.
- Renewals fail at form validation, so the renew button in the
  frontend does nothing.
- Every `.lcpl` download leaks an API-equivalent secret into URL
  history.
- The two teams will keep rediscovering integration mismatches at
  meetings instead of in PR review.

## Proposed Solution

### Overview

Six clusters, each independently shippable. A→B→C are the lifecycle
core (must land together to fix the meeting's borrow/renew/return
loop). D, E, F are companion work that can ship in subsequent PRs.

### Key Components

1. **`LicenseAction` enum + form split** (A) — top-level enum at
   `apps/readium/enums.py` (Q5); the PUT form validates against
   actions (`active / returned / renewed / revoked / cancelled`),
   not states. The renew action accepts `requested_end` only;
   `duration` is a one-release legacy shim (Q1).
2. **`expire_lapsed_licenses` Celery beat + on-read fallback** (B) —
   one place transitions `ACTIVE/READY → EXPIRED` when `expires_at <
   now()`; `can_user_borrow` refetches+reconciles before raising the
   "already has active license" error.
3. **`StatusServerSyncService` with read-after-write** (C) — the
   single dispatch point promised in IP-008 Q1; every catalog-driven
   LSD mutation finishes with a `GET` + state-from-document
   reconcile.
4. **`CapabilityTokenService` for `.lcpl` downloads** (D) — Redis-
   backed token layer in `apps/core/services/` (Q2). Two scopes:
   `lcpl_download` (single-use, 60s) for direct UI downloads and
   `lcpl_feed_download` (multi-use peek, 30 min) for OPDS feed
   serialization. `LicenseDownloadView` is **token-only**; Bearer
   on `.lcpl` is hard-cut at Phase 4 ship with deploy ordering as
   the only coordination (Q3).
5. **`License.renewal_count` + policy** (E) — model field + serializer
   surface + `EVILFLOWERS_READIUM_MAX_RENEWALS` policy in
   `evaluate_renew`. Default `None` = uncapped (Q6).
6. **`docs/readium/integration-contract.md`** (F) — single page
   documenting every endpoint, payload shape, expected response,
   client-action mapping, and what the inlined `Entry` LCP fields
   mean.
7. **elvira-portal GitHub issues G1–G8** (Phase 7) — eight
   tracked frontend-side fixes filed against
   `EvilFlowersCatalog/elvira-portal` `dev` so the portal team can
   sequence and close them independently of EFC PRs.

### Architecture

```mermaid
flowchart TB
    subgraph LIFECYCLE[License lifecycle]
        ACTION[/PUT /licenses/{id}<br/>action=renewed<br/>requested_end=iso/] --> FORM[UpdateLicenseForm<br/>validates LicenseAction]
        FORM --> SVC[LicenseService.renew_license<br/>new_end_date=...]
        SVC --> SYNC[StatusServerSyncService.renew]
        SYNC --> LSD[(LSD PATCH)]
        SYNC --> GET[LSD GET status]
        GET --> RECONCILE[Reconcile License.state,<br/>device_count, expires_at]
    end
    subgraph EXPIRY[Expiry reconcile]
        BEAT[Celery beat<br/>expire_lapsed_licenses] --> SWEEP[transition ACTIVE/READY → EXPIRED<br/>when expires_at < now]
        BORROW[/POST /licenses/] --> CHECK[can_user_borrow]
        CHECK -->|stale ACTIVE found| RECONCILE2[refetch LSD<br/>or mark EXPIRED locally]
        RECONCILE2 --> CHECK
    end
    subgraph DOWNLOAD[.lcpl download — token-only]
        MINT[/POST /licenses/{id}/download-tokens/] --> CAPS[CapabilityTokenService<br/>scope=lcpl_download<br/>Redis TTL 60s]
        FEED[OPDS feed render] --> CAPF[CapabilityTokenService<br/>scope=lcpl_feed_download<br/>Redis TTL 30 min, peek]
        CAPS --> URL[GET .lcpl?token=...]
        CAPF --> URL
        URL --> CHECK_SCOPE[scope=lcpl_download → consume<br/>scope=lcpl_feed_download → peek]
        CHECK_SCOPE --> SERVE[fresh .lcpl bytes]
        URL -.->|Bearer / ?access_token=| REJECT[401 → mint endpoint]
    end
    style FORM fill:#cfc,stroke:#393
    style RECONCILE fill:#cfc,stroke:#393
    style SWEEP fill:#cfc,stroke:#393
    style CAPS fill:#cfc,stroke:#393
    style CAPF fill:#cfc,stroke:#393
    style REJECT fill:#fcc,stroke:#933
```

## Implementation Plan

### Phase 1: Lifecycle Vocabulary (Cluster A)

- [ ] **A1** — Introduce top-level `LicenseAction` enum at
  `apps/readium/enums.py::LicenseAction` (Q5 resolution). Members:
  `ACTIVATE = "active"`, `RETURN = "returned"`, `RENEW = "renewed"`,
  `REVOKE = "revoked"`, `CANCEL = "cancelled"`. Module-level so
  forms / serializers / services import without circular pressure on
  `apps/readium/models.py`.
- [ ] **A1** — Rewrite `UpdateLicenseForm`
  (`apps/readium/forms.py:14`) to validate against
  `LicenseAction.choices`. Rename the field to `action` for clarity;
  accept legacy `state` for one release with a `DeprecationWarning`
  log line.
- [ ] **A1** — Rename `LicenseDetail._handle_state_change` →
  `_dispatch_action` and pivot on `LicenseAction` values. The map
  state → action is no longer needed.
- [ ] **A2′** — Canonicalize renew payload on `requested_end` (Q1
  resolution). `UpdateLicenseForm` for the renew action accepts
  `requested_end: ISO-8601` only. Legacy `duration` payloads are
  translated to `requested_end = now() + duration` with a single
  deprecation log per request — no Sunset header, removed in the
  release after Phase 1 ships. `LicenseService.renew_license` is
  invoked with `new_end_date=` only; the `new_duration_days=`
  positional argument is deprecated and removed one release later.
  Both `PUT /licenses/{id}` (action=renewed) and
  `POST /licenses/{id}/renewals` route through one
  `evaluate_renew(license, requested_end=…) → renew_license(license,
  new_end_date=…)` path.
- [ ] **A3** — `apps/readium/tests/test_lifecycle_vocabulary.py`:
  - PUT with each action returns 200 and applies the right
    `LicenseService` call.
  - PUT with `state: "active"` returns 200 once and logs a
    deprecation warning.
  - PUT with `action: "renewed"` + `requested_end` succeeds.
  - PUT with `action: "renewed"` + legacy `duration`: succeeds AND
    emits a deprecation log entry (assert via `assertLogs`).
  - PUT with `action: "renewed"` + invalid window 403s through
    `evaluate_renew`.

### Phase 2: Expiry Reconciliation (Cluster B)

- [ ] **B1** — New Celery beat task
  `apps/readium/tasks.py::expire_lapsed_licenses` (every 5 minutes).
  Scope: `License.objects.filter(state__in=[READY, ACTIVE],
  expires_at__lt=now()).select_for_update()` → set `state=EXPIRED`,
  `save(update_fields=["state","updated_at"])`. Call
  `LicenseService._maybe_promote_next(license)` per row.
- [ ] **B1** — Register the task in
  `evil_flowers_catalog/celery.py` beat schedule alongside
  `sweep_unclaimed_reservations`.
- [ ] **B2** — `LicenseService.can_user_borrow`
  (`apps/readium/services/license_service.py:170`): before raising
  "User already has an active license," check
  `existing_license.expires_at < now()`. If true, transition in the
  same transaction (call into `StatusServerSyncService.reconcile`
  below — read LSD, accept LSD's verdict, fall back to local EXPIRED
  if LSD unreachable). Re-evaluate availability after reconcile.
- [ ] **B3** — Tests:
  `apps/readium/tests/test_expiry_reconcile.py`:
  - Beat task transitions ACTIVE-past-end → EXPIRED, partial
    unique-constraint releases.
  - Concurrent re-borrow + sweep: re-borrow succeeds.
  - `can_user_borrow` with stale ACTIVE: reconciles + permits new
    borrow.
- [ ] **B4** — Backfill management command
  `apps/readium/management/commands/reconcile_expired_licenses.py
  [--dry-run] [--catalog NAME]` for one-shot cleanup of existing
  rows on deploy.

### Phase 3: LSD Read-After-Write (Cluster C)

- [ ] **C1** — Introduce
  `apps/readium/services/status_server_sync.py::StatusServerSyncService`
  as the single dispatch point. Public methods: `return_license`,
  `renew_license`, `revoke_license`, `cancel_license`,
  `register_device`, `reconcile(license)`. All but `reconcile`
  internally `1. PATCH; 2. GET status; 3. _apply_lsd_document(license,
  doc)`.
- [ ] **C1** — `_apply_lsd_document(license, doc)` maps LSD `status`
  (`active / ready / returned / revoked / cancelled / expired`) to
  `License.LicenseState`, updates `device_count` from
  `doc["events"]` aggregate (count of `register` minus `return`
  events, clamped ≥ 0), and updates `expires_at` from
  `doc["potential_rights"]["end"]` when present.
- [ ] **C1** — Move `StatusServerClient` to a *transport* layer
  (`apps/readium/services/lsd_transport.py`) — pure HTTP, no DB
  writes. `StatusServerSyncService` orchestrates transport + DB.
- [ ] **C2** — Replace every callsite of `StatusServerClient.return_license` /
  `renew_license` / `revoke_license` / `cancel_license` /
  `register_license` with `StatusServerSyncService`. Audit:
  `LicenseService` (return/renew/revoke/cancel), `ReturnProxyView`
  (already mirrors locally — replace the mirror block with
  `sync_service.reconcile(license, lsd_doc=data)` so we don't
  GET twice), `DeviceRegistrationProxyView` (same).
- [ ] **C3** — Tests
  `apps/readium/tests/test_lsd_sync_reconcile.py`:
  - PATCH succeeds, GET returns differing state → local row reflects
    GET.
  - PATCH 409 → no local write, 502 surfaced.
  - GET 5xx after successful PATCH → log error, keep optimistic
    write, emit metric `lsd.reconcile.fallback`.
  - LSD `events` count drives `device_count`.

### Phase 4: Capability Tokens for `.lcpl` Downloads (Cluster D)

Per Q2 resolution, Phase 4 is promoted from a model-backed
single-purpose token to a **generalized capability-token layer** that
reuses the existing Django/Redis cache (already used by
`apps/api/views/tokens.py` for refresh-token sessions). No DB model,
no migration, no purge task — Redis TTL handles eviction natively.

- [ ] **D1** — New module
  `apps/core/services/capability_tokens.py::CapabilityTokenService`:
  - Public API:
    - `mint(scope: str, subject: dict, ttl: int, *, single_use: bool = True) -> str`
    - `consume(token: str, expected_scope: str) -> dict | None`
      (atomic `cache.delete`-after-read; returns payload or `None`)
    - `peek(token: str, expected_scope: str) -> dict | None`
      (read-only; for multi-use feed tokens)
  - Storage: `django.core.cache` with key namespace
    `cap:{scope}:{token}`.
  - Token format: `secrets.token_urlsafe(32)`.
  - Payload (JSON-serialized): `{"sub": user_id, "scope": "...",
    "resource_id": "...", "single_use": bool, "issued_at": <iso>}`.
  - Tests: `apps/core/tests/test_capability_tokens.py` —
    scope/TTL/single-use vs peek/cross-scope rejection/expiry.
- [ ] **D1** — Scope registry in
  `apps/readium/capability_scopes.py`: declares the two scopes used
  in this proposal:
  - `LCPL_DOWNLOAD` — single-use, TTL
    `EVILFLOWERS_CAPABILITY_TOKEN_LCPL_TTL_SECONDS` (default 60s).
    Issued by `POST /licenses/{id}/download-tokens`.
  - `LCPL_FEED_DOWNLOAD` — multi-use, TTL
    `EVILFLOWERS_CAPABILITY_TOKEN_LCPL_FEED_TTL_SECONDS` (default
    1800s = 30 min). Issued inline by feed serialization.
- [ ] **D2** — New endpoint
  `apps/readium/views/download_tokens.py::DownloadTokenView`:
  - `POST /readium/v1/licenses/{license_id}/download-tokens` — Bearer-
    authenticated. Verifies `check_license_download` via
    `object_checker`. Calls
    `CapabilityTokenService.mint("lcpl_download", {"sub": user.pk,
    "resource_id": str(license.pk)}, ttl=settings...)`. Returns
    `{token, download_url, expires_at}` where `download_url` is the
    absolute `…/readium/v1/licenses/{id}.lcpl?token=<token>`.
- [ ] **D2** — `LicenseDownloadView.get`
  (`apps/readium/views/download.py:48`): per Q3 resolution, rewire
  to **token-only** auth (no longer extends `SecuredView`; extends
  `django.views.View`). Reads `?token=…` query param, calls
  `CapabilityTokenService.consume(token, expected_scope=
  "lcpl_download")` *or* `.peek(token, expected_scope=
  "lcpl_feed_download")` based on the payload's `scope`. Rejects
  missing/expired token with 401 + RFC 7807 problem-detail pointing
  to the mint endpoint. Verifies `payload["resource_id"] ==
  str(license_id)` to prevent cross-license replay.
- [ ] **D2** — `LicenseSerializer.Base.download_url` (computed):
  - When `serializer_context.get("opds_feed") is True`, mint a
    `LCPL_FEED_DOWNLOAD` token (longer TTL, peek-able) and embed in
    URL.
  - When `request` is present (single-license response), mint a
    `LCPL_DOWNLOAD` token (60s, single-use).
  - When no context, return the relative path without token
    (operator scripts, tests; will fail at the consume step).
  - OPDS view code sets `opds_feed=True` in the serializer context;
    `feed_builder` is the natural place.
- [ ] **D3** — Hard cut on Bearer/`?access_token=…` for `.lcpl`
  (Q3 resolution). `LicenseDownloadView` stops extending
  `SecuredView`; the only accepted credential is `?token=…`. No
  feature flag, no Sunset header. Cross-repo ordering is the
  coordination mechanism: portal-side G1 must ship before this
  Phase 4 PR merges (tracked by Phase 7 G8).
- [ ] **D4** — Tests
  `apps/readium/tests/test_lcpl_download.py`:
  - `POST .../download-tokens` mints; the token resolves a single
    `.lcpl` body; second use → 401.
  - Feed-context serialization produces a multi-use token (peek
    twice within TTL succeeds; after TTL → 401).
  - Cross-license token → 401 (`resource_id` mismatch).
  - Cross-scope token (a `LCPL_DOWNLOAD` reused as a feed token)
    → 401.
  - Bearer/`?access_token=` against `.lcpl` → 401 with a
    problem-detail body whose `detail_type` points at the mint
    endpoint. No regression: Bearer on every *other* Readium
    endpoint still works.
- [ ] **D5** — Document the migration in the integration contract
  doc (Phase 6): elvira-portal moves from URL-baked JWT to
  `mint → open`. Cross-reference Phase 7 G1 and G8.

### Phase 5: Renewal Counter (Cluster E)

- [ ] **E1** — `License.renewal_count = PositiveIntegerField(default=0)`.
  Migration `apps/readium/migrations/0008_*.py` backfills `0`.
- [ ] **E1** — `LicenseService.renew_license` increments
  `renewal_count` inside the same transaction as the state update.
- [ ] **E1** — `LicenseSerializer.Base` exposes `renewal_count` and a
  computed `renewals_remaining` (uses
  `EVILFLOWERS_READIUM_MAX_RENEWALS`).
- [ ] **E2** — `evaluate_renew` (`apps/readium/services/renew_policy.py`)
  rejects when `license.renewal_count >=
  settings.EVILFLOWERS_READIUM_MAX_RENEWALS` (default `None` = no
  cap, opt-in per deployment).
- [ ] **E3** — Tests
  `apps/readium/tests/test_renewal_count.py`:
  - Renew bumps the counter exactly once per successful PATCH.
  - At-cap rejects with `RenewDecision.allowed=False, reason=
    "Renewal cap reached (N/N)"`.
  - Serializer exposes both fields; pagination response includes
    them.

### Phase 6: Integration Contract Documentation (Cluster F)

- [ ] **F1** — `docs/readium/integration-contract.md` (new). Single
  page; sections:
  - *Authentication*: Bearer JWT for all endpoints **except** the
    `.lcpl` download, which is **token-only** (capability token via
    `?token=…`, minted from `POST .../download-tokens`). Per Q3
    resolution, the cut is unconditional once Phase 4 ships — no
    EFC feature flag; deploy ordering (portal G1 first, EFC Phase
    4 second) is enforced by Phase 7 G8.
  - *Borrow flow*: `POST /readium/v1/licenses` → 201 with serialized
    License → frontend calls `POST .../download-tokens` → opens the
    returned `download_url` in `thorium://` or a new tab.
  - *Renew flow*: `PUT /readium/v1/licenses/{id}` body
    `{action: "renewed", requested_end: "<iso-8601>"}` (Q1
    resolution — `duration` is deprecated and will be removed). The
    Status Server's `renew_custom_url` callback uses
    `POST /licenses/{id}/renewals` with the same body shape.
  - *Return flow*: `PUT /readium/v1/licenses/{id}/return` (LSD
    proxy — what reader apps call) *and* `PUT
    /readium/v1/licenses/{id}` with `{action: "returned"}` (what UI
    calls). No PATCH variant exists.
  - *Lifecycle actions table*: `LicenseAction` × required body
    fields × resulting `LicenseState`.
  - *Entry pagination contract*: catalog of LCP-aware fields on
    `EntrySerializer` (`lcp_state`, `available_slots`,
    `total_slots`, `active_count`, `over_saturated`,
    `next_available_at`, `user_active_license_id`, `queue_length`,
    `user_reservation_id`, `user_position`) — each documented with
    a one-line "use it for X" hint.
  - *Reservation flow*: `POST /readium/v1/reservations`,
    `PATCH /reservations/{id}` with `{status: "cancelled" | "claimed"}`.
    Note: the *only* PATCH in the Readium surface.
  - *Errors*: RFC 7807 problem-detail catalogue used by Readium
    endpoints (passphrase_required, conflict, forbidden, validation_error).
- [ ] **F2** — `docs/readium/elvira-portal-action-items.md` (new,
  short). Concrete list of frontend-side fixes derived from this
  proposal:
  - Switch `useDownloadLicense` to mint a download token first.
  - Drop `item.lcp_license_id || item.id` fallback; always use
    `item.id` or the new `download_url` field.
  - Consume the inlined `EntrySerializer` LCP fields in entry list
    + detail; remove redundant `/availability` calls where the
    inlined data suffices.
  - Update TS `LICENSE_STATE` (fix `draft → ready`) and add a
    `LICENSE_ACTION` enum that mirrors the backend.
- [ ] **F3** — Link the new docs from `docs/proposals/index.md` and
  add a `docs/readium/` mkdocs nav entry.

### Phase 7: elvira-portal GitHub Issues (Frontend Hand-off)

The portal-side fixes derived from this proposal are tracked as
discrete GitHub issues on the `EvilFlowersCatalog/elvira-portal`
repository so the frontend team can pick them up, sequence them, and
close them independently of EFC PRs. Every issue is filed against the
`dev` branch context (the branch the team is integrating on),
references this proposal (IP-009) in the body, and is labelled
`integration`, `readium`. The list below is the source of truth — the
backend developer (Jakub) creates them all at once when this proposal
flips to Accepted, using `gh issue create` against the elvira-portal
repo. Each issue gets a checkbox here so progress is mirrored back
into the proposal.

- [x] **G1** — Filed: [elvira-portal#5](https://github.com/EvilFlowersCatalog/elvira-portal/issues/5)
  - **Body**: `useDownloadLicense.tsx` currently embeds the user's
    full Bearer JWT in the URL `?access_token=…` (long-lived,
    leaks to history / referrer / proxy logs). After IP-009 Phase
    4, `.lcpl` accepts a single-use capability token only.
  - **Required changes**: rewrite `useDownloadLicense` to
    (1) `POST /readium/v1/licenses/{id}/download-tokens`,
    (2) read `download_url` from the response,
    (3) hand that URL to the `thorium://` link / new-tab opener.
  - **Acceptance**: no `auth.token` in any `.lcpl`-bound URL;
    `dev` smoke-test: download works against the EFC Phase 4
    branch (token-only `.lcpl`).
  - **Labels**: `integration`, `readium`, `security`.

- [x] **G2** — Filed: [elvira-portal#6](https://github.com/EvilFlowersCatalog/elvira-portal/issues/6)
  - **Body**: `LoansTable.tsx` line
    `const licenseId = item.lcp_license_id || item.id;` then
    `openInThorium(licenseId)`. The backend route resolves the
    **local** license PK (`item.id`), not the LCP-server UUID.
    Whenever `lcp_license_id` is populated (post-issuance), the URL
    404s.
  - **Required changes**: always use `item.id`; better, consume
    the new `download_url` field returned from the mint endpoint
    (G1) instead of building URLs client-side.
  - **Acceptance**: existing loan with non-null `lcp_license_id`
    downloads on first click without a 404.
  - **Labels**: `integration`, `readium`, `bug`.

- [x] **G3** — Filed: [elvira-portal#7](https://github.com/EvilFlowersCatalog/elvira-portal/issues/7)
  - **Body**: `src/utils/interfaces/licenses.ts` carries `draft`
    (typo for `ready`) and no `renewed`. The form/select widgets
    in `AdminLoans.tsx` use a hardcoded list that does not match.
  - **Required changes**:
    - Fix typo: `draft` → `ready`.
    - Add `enum LICENSE_ACTION { active, returned, renewed,
      revoked, cancelled }` mirroring the new EFC `LicenseAction`.
    - `useUpdateLicense` posts `{action: …}` (not `{state: …}`).
  - **Acceptance**: typecheck passes; renewal action UI sends
    `action: "renewed"` and EFC returns 200.
  - **Labels**: `integration`, `readium`, `tech-debt`.

- [x] **G4** — Filed: [elvira-portal#8](https://github.com/EvilFlowersCatalog/elvira-portal/issues/8)
  - **Body**: Per IP-009 Q1 resolution, the canonical renew payload
    is `{action: "renewed", requested_end: "<iso-8601>"}`. EFC
    deprecates `duration`.
  - **Required changes**: replace `'P1Y'`-style ISO duration in
    `AdminLoans.tsx` with an ISO datetime, picked via a date
    picker bound to `evaluate_renew`'s window (the contract doc
    F1 publishes the valid range).
  - **Acceptance**: PUT `/licenses/{id}` with `{action: "renewed",
    requested_end}` returns 200; legacy `duration` payload no
    longer sent from any code path.
  - **Labels**: `integration`, `readium`.

- [x] **G5** — Filed: [elvira-portal#9](https://github.com/EvilFlowersCatalog/elvira-portal/issues/9)
  - **Body**: The 405 reported in the 2026-05-25 meeting indicates
    a deployed bundle was calling PATCH. `dev` HEAD already uses
    PUT; ensure no other code path or deployment is PATCH-bound.
  - **Required changes**: audit the codebase for any `axios.patch(
    "/readium/v1/licenses/…")`; redeploy the dev/stage build so
    Tomáš's traced 405 disappears.
  - **Acceptance**: returning an active loan from the portal
    succeeds; backend logs show PUT.
  - **Labels**: `integration`, `readium`, `bug`.

- [x] **G6** — Filed: [elvira-portal#10](https://github.com/EvilFlowersCatalog/elvira-portal/issues/10)
  - **Body**: EFC already inlines `lcp_state`, `available_slots`,
    `total_slots`, `active_count`, `over_saturated`,
    `next_available_at`, `user_active_license_id`, `queue_length`,
    `user_reservation_id`, `user_position` on every entry list and
    detail response (see contract doc F1). The portal currently
    calls `/readium/v1/entries/{id}/availability` to derive the
    same data.
  - **Required changes**:
    - Extend the TS `IEntry` type with the inlined fields.
    - In entry-detail and entry-card components, render
      "available / full / borrowed by you / queue: N (you are #M)"
      from the inlined fields.
    - Keep `/availability` only for the calendar widget that
      needs per-day data.
  - **Acceptance**: entry-detail page reads loan/queue state from
    a single round-trip; network panel shows no `/availability`
    call on simple browse.
  - **Labels**: `integration`, `readium`, `performance`.

- [x] **G7** — Filed: [elvira-portal#11](https://github.com/EvilFlowersCatalog/elvira-portal/issues/11)
  - **Body**: Phase 5 exposes `renewal_count` and
    `renewals_remaining` on `LicenseSerializer.Base`. Portal
    should display "Renewed X times" + "X renewals remaining
    (cap: N)" on the loan card / detail.
  - **Required changes**: type extension + UI rendering;
    disable the "Renew" button when `renewals_remaining === 0`.
  - **Acceptance**: loan detail shows renewal history; renew
    button greys out at cap.
  - **Labels**: `integration`, `readium`, `enhancement`.

- [x] **G8** — Filed: [elvira-portal#12](https://github.com/EvilFlowersCatalog/elvira-portal/issues/12)
  - **Body**: Per IP-009 Q3 resolution, Phase 4 hard-cuts Bearer
    auth on `.lcpl`. There is no EFC feature flag — the only
    coordination is repo merge order. Portal G1 must land and be
    deployed to the environment **before** the EFC Phase 4 PR
    merges to `develop`. This issue is the cross-repo tracker so
    both teams hold the line.
  - **Required changes**: ops checklist (portal `dev` deploy →
    portal `master`/prod deploy → EFC `develop` merge → EFC
    deploy) + smoke-test playbook referenced from the
    integration-contract doc.
  - **Acceptance**: production access logs show zero Bearer
    `.lcpl` requests in the 7 days following the EFC deploy.
  - **Labels**: `integration`, `readium`, `ops`.

**Owner**: Jakub Dubec opens the issues from the EFC side
(`gh issue create -R EvilFlowersCatalog/elvira-portal …`) on the
PR-merge of this proposal; Tomáš Kordoš (`@kordostomas`) is
assigned to each. Cross-reference: every issue body cites the
proposal commit, the specific section (Cluster D/F mostly), and the
EFC PR(s) it pairs with.

## Technical Details

### Data Model Changes

```python
# apps/readium/enums.py (new — per Q5 resolution)
from django.db import models
from django.utils.translation import gettext_lazy as _


class LicenseAction(models.TextChoices):
    ACTIVATE = "active", _("Activate")
    RETURN = "returned", _("Return")
    RENEW = "renewed", _("Renew")
    REVOKE = "revoked", _("Revoke")
    CANCEL = "cancelled", _("Cancel")


# apps/readium/models.py
class License(BaseModel):
    # ... existing fields ...
    renewal_count = models.PositiveIntegerField(default=0)
```

Per Q2 resolution, capability tokens are NOT a database model.
`CapabilityTokenService` (`apps/core/services/capability_tokens.py`)
stores payloads in `django.core.cache` (Redis) under the namespace
`cap:{scope}:{token}`; TTL is enforced by Redis. The two scopes used
in this proposal:

| Scope | Mode | TTL | Resource |
|---|---|---|---|
| `lcpl_download` | single-use (consume) | 60s (configurable) | license `id` |
| `lcpl_feed_download` | multi-use (peek) | 1800s (configurable) | license `id` |

### API Changes

| Method | Path | Action |
|---|---|---|
| POST | `/readium/v1/licenses/{id}/download-tokens` | Mint capability token (`lcpl_download` scope, single-use, 60s) — new |
| GET | `/readium/v1/licenses/{id}.lcpl?token=…` | **Token-only** download (Q3 hard cut — Bearer/`?access_token=` rejected with 401 pointing to the mint endpoint) |
| PUT | `/readium/v1/licenses/{id}` | Body `{action: "active"\|"returned"\|"renewed"\|"revoked"\|"cancelled", …}`. New vocabulary; `state` deprecated for one release. Renew accepts `requested_end` (canonical) or `duration` (legacy, translated; one-release shim per Q1). |

OPDS 1.2/2.0 acquisition feeds: when `LicenseSerializer.Base` is
serialized inside `serializer_context["opds_feed"]=True`, the
`download_url` carries a `lcpl_feed_download`-scoped token (multi-use
peek, 30 min TTL) so a feed read by a reading app needs no extra
mint round-trip within the window.

### Configuration

```sh
# Renewal policy
EVILFLOWERS_READIUM_MAX_RENEWALS=          # default unset (no cap; Q6 resolution)

# Capability tokens
EVILFLOWERS_CAPABILITY_TOKEN_LCPL_TTL_SECONDS=60
EVILFLOWERS_CAPABILITY_TOKEN_LCPL_FEED_TTL_SECONDS=1800

# Existing knobs unchanged: MAX_RENEW_DAYS, RENEW_EMBARGO_DAYS, EXPIRY_REMINDER_DAYS.
```

### Celery Beat Additions

```python
# evil_flowers_catalog/celery.py
app.conf.beat_schedule.update({
    "expire-lapsed-licenses": {
        "task": "apps.readium.tasks.expire_lapsed_licenses",
        "schedule": crontab(minute="*/5"),
    },
})
```

Per Q2 resolution, capability tokens are evicted by Redis TTL — no
purge task is needed.

## Alternatives Considered

### Alternative 1: Keep `state: "renewed"` semantics, just add `renewed` to LicenseState

**Pros**: One enum, no migration.

**Cons**: Semantically wrong — a renewed license is `active`, not
`renewed`. Conflates the resting state with the verb that produced it.
Also leaks into the LSD mapping, which has no `renewed` status.

**Why not chosen**: The mental model is exactly two enums (state,
action). Refusing to model it is the source of the bug.

### Alternative 2: On-read reconcile only, no Celery beat

**Pros**: Less infrastructure.

**Cons**: `LicenseFilter.qs` (`apps/readium/filters/licenses.py`) is
used by reporting / pagination; lazy reconcile per row makes list
endpoints O(N·LSD-roundtrip). The Celery beat is amortized cost.

**Why not chosen**: The beat task is ~30 lines; cheap to maintain.

### Alternative 3: Refresh-token pattern instead of capability tokens

**Pros**: Familiar OAuth2 model.

**Cons**: Refresh tokens are long-lived and bearer-scoped. The whole
point of the `lcpl_download` capability is that it has the *scope* of
one license, not the user's API surface, and it's consumed after one
use.

**Why not chosen**: Capability tokens are not auth — they're scoped
to a single resource. Single-use + 60s TTL matches that.

### Alternative 5: Persist `LicenseDownloadToken` in the database

**Pros**: Auditable trail of every download mint.

**Cons**: Adds a DB model, a migration, a unique-index, an hourly
purge task, and `select_for_update` contention on the consume path —
all to reimplement what Redis TTL gives us for free. The existing
`apps/api/views/tokens.py` already proves the pattern (`refresh_token:
{jti}` via `django.core.cache`).

**Why not chosen** (Q2 resolution): generalize the Redis-backed
capability layer once; reuse the scope abstraction for future flows
(Dataverse redirects, etc.). Audit can land later as an event emit
from `CapabilityTokenService.consume`.

### Alternative 4: Move renewal_count to a separate `LicenseRenewal` audit table

**Pros**: Full history.

**Cons**: Audit history can come later (IP-008 future-work item:
`audit_license_state` command). For STU's "max 2 renewals" rule we need
the count, not the history.

**Why not chosen**: YAGNI for now; the `renewal_count` integer field
covers the policy; future audit work can backfill from
`StatusServerSyncService` event hooks.

## Trade-offs and Risks

### Trade-offs

- **Two enums (state vs action)** means model code touches both. Worth
  it — the alternative (action-as-state) keeps causing meeting bugs.
- **On-read reconcile in `can_user_borrow`** adds an LSD round-trip
  to the borrow path *only when stale state is detected*. Acceptable.
- **Token-only `.lcpl`** adds one round-trip (mint → open) vs. the
  current URL-baked JWT. Worth it for the secret-exposure delta —
  the long-lived bearer token no longer reaches browser history,
  referrer headers, or proxy access logs.
- **Hard cut on Bearer (Q3)** costs a coordinated cross-repo deploy
  (portal G1 first → EFC Phase 4 second). Cheaper than carrying a
  feature-flag and its sunset path through a release.
- **`renewal_count` on the row** vs. an audit table — see Alternative 4.

### Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Beat task races with active borrow on the same entry | Low | Beat acquires `select_for_update` per License; borrow already uses Entry-level lock. Test for combined contention. |
| LSD GET after PATCH returns a different status than expected | Medium | Reconcile trusts LSD; record metric `lsd.reconcile.divergence{action}` so divergence is observable. |
| Existing dev/staging data has lapsed `ACTIVE` rows that fail the new unique constraint after reconcile | Medium | `reconcile_expired_licenses` management command lists rows in dry-run first; operator runs it before enabling the beat. |
| Frontend deploys `action`-vocabulary before backend ships | Low | Backend accepts both `state` and `action` for one release with a deprecation warning. |
| EFC Phase 4 deploys before portal G1 → all portal downloads break | High | Phase 7 G8 enforces ordering; backend PR description blocks merge on portal G1 already being live on dev. Operator runbook checks `?access_token=` access logs trending to zero before flipping production. |
| Redis loses capability token state on restart (cache eviction) | Low | Tokens have ≤30 min TTL anyway; clients re-mint. Document expected behaviour in F1. |
| `renewal_count` increment skipped if `renew_license` raises after the increment | Low | Increment is in the same `transaction.atomic` as the state mutation; rollback restores it. |

## Success Criteria

- [ ] elvira-portal can renew a license via `PUT /readium/v1/licenses/{id}`
  with `{action: "renewed", requested_end: "<iso-8601>"}` and receive 200.
  Legacy `duration` payload also succeeds with one deprecation-log
  line.
- [ ] A user whose previous loan's `expires_at` has passed can take a
  new loan on the same entry without manual cleanup (Cluster B).
- [ ] After `LicenseService.return_license(license)`, a fresh `GET
  /readium/v1/licenses/{id}/status` reflects the returned state and
  the local `License.state` equals the LSD document's status
  (Cluster C).
- [ ] `POST /readium/v1/licenses/{id}/download-tokens` returns a
  `lcpl_download`-scoped token consumable exactly once via
  `?token=...`; second use → 401.
- [ ] OPDS feed serialization carries an `lcpl_feed_download` token
  in `download_url`; reader app opens the URL within 30 min without
  a fresh mint round-trip.
- [ ] `GET /.lcpl` with Bearer / `?access_token=…` returns 401 +
  problem-detail pointing to the mint endpoint (Q3 hard cut).
- [ ] `License.renewal_count` increments by exactly one per
  successful renewal; serializer exposes `renewal_count` and
  `renewals_remaining` (the latter is `None` when the cap is unset).
- [ ] `docs/readium/integration-contract.md` is published and linked
  from `docs/proposals/index.md`; `docs/readium/` mkdocs nav entry
  is live.
- [ ] All eight elvira-portal issues G1–G8 are filed against
  `EvilFlowersCatalog/elvira-portal` `dev` on proposal acceptance,
  each linked back to its proposal section.
- [ ] All meeting bugs from the 2026-05-25 review are individually
  green-boxed (one PR comment per bug citing the closing commit).

## Future Considerations

- **Audit table `LicenseRenewalRecord`** (per Alternative 4) — full
  history of who renewed what when, for SLA reporting.
- **WebSocket / SSE channel for LSD events** — push reconciles
  instead of polling.
- **Additional `CapabilityTokenService` scopes** — Dataverse external-URL
  downloads, image/thumbnail tokenization, OCR result fetch, etc.
  Phase 4 introduces the layer; future flows reuse it by registering
  new scopes.
- **Multi-tenant LSD segregation** (already in IP-008 future-work).
- **`manage.py audit_license_state`** — already in IP-008 future
  work; would consume `StatusServerSyncService` introduced in
  Phase 3.
- **Remove the `duration` legacy shim** — one release after Phase 1
  ships, drop the translation logic + deprecation log; `requested_end`
  becomes the only accepted renew payload field.

## References

- `apps/readium/forms.py:14-16` — `UpdateLicenseForm.state` validates
  against wrong enum (A1).
- `apps/readium/views/licenses.py:161-221` — `_handle_state_change`
  dispatches on values the form rejects (A1).
- `apps/readium/models.py:69-75, 103-107` — `LicenseState` enum;
  `is_expired` Python property (B1). New top-level `LicenseAction`
  lives at `apps/readium/enums.py` per Q5.
- `apps/readium/services/license_service.py:170-181` —
  `can_user_borrow` ignores stale ACTIVE (B2).
- `apps/readium/services/status_server_client.py:92-208` — PATCH
  without read-after-write (C1).
- `apps/readium/views/status_proxy.py:143-197` — IP-008 C6 mirror;
  reuse for `StatusServerSyncService` (C2).
- `apps/core/views.py:21-47` — `SecuredView` accepts `?access_token=…`
  (D1). After IP-009 `LicenseDownloadView` no longer extends
  `SecuredView`.
- `apps/api/views/tokens.py:48,73` — existing Redis-cache token
  pattern (`refresh_token:{jti}`); `CapabilityTokenService` reuses
  the same `django.core.cache` backend with a `cap:{scope}:` key
  namespace (Q2).
- `apps/readium/views/download.py:48-98` — `.lcpl` download view;
  rewired to token-only auth (D2).
- `apps/readium/services/renew_policy.py:32-66` — `evaluate_renew`
  to extend with renewal cap (E2).
- `apps/api/serializers/entries.py:106-145` — inlined LCP fields on
  `EntrySerializer.Base` (F1).
- `apps/readium/services/entry_lcp_decorator.py` — `lcp_state_mapping`
  populates the inlined fields (F1).
- `docs/proposals/posts/ip-008-readium-correctness-followup.md` —
  Baseline. Q1 resolution (LSD lifecycle sync) is the design IP-009
  Phase 3 implements.
- elvira-portal `src/hooks/api/licenses/useDownloadLicense.tsx`,
  `src/components/items/loans/LoansTable.tsx`,
  `src/utils/interfaces/licenses.ts`,
  `src/pages/admin/AdminLoans.tsx` — frontend integration surfaces
  referenced by Cluster D and F; tracked by Phase 7 G1–G7.

## Review Questions

**Status**: ✅ Resolved
**Review Date**: 2026-05-25
**Resolution Date**: 2026-05-25
**Reviewer**: Claude AI

The following questions were resolved on 2026-05-25; each `Resolution`
block captures the decision and how the Implementation Plan / Technical
Details / API Changes were updated to match.

---

### Q1 ⚠️: Renewal payload — accept both `duration` and `requested_end` or canonicalize on one?

**Issue**: After IP-008 the renewals sub-resource takes `requested_end`;
the `PUT /licenses/{id}` path takes `duration`. Phase 1 A2 proposes
accepting both on the PUT path to ease migration.

**Question**: Both or just one?

**Options**:

- [ ] **A**: Accept both on PUT for one release; deprecate
  `duration` in favour of `requested_end` (Recommended). Matches
  LSD's `renew_custom_url` shape and removes day-rounding logic.
- [X] **B**: Canonicalize on `requested_end` immediately; require
  elvira-portal to switch.
- [ ] **C**: Canonicalize on `duration` (simpler for the frontend);
  drop the `requested_end` payload on the renewals sub-resource.

**Answer**:

```
I am open to changes on both portal and backend. Focus on the best UX.
```

**Resolution**:

```
Option B chosen — canonicalize on `requested_end` immediately.
`duration` becomes a UX-side concept only ("two weeks", "until Friday");
both the portal and the LSD `renew_custom_url` callback compute an exact
ISO-8601 datetime and post `{action: "renewed", requested_end: <iso>}`.

Rationale:
  - LSD §6.5 already speaks `end=<iso>` over the wire; the renewals
    sub-resource matches this; PUT becomes consistent.
  - One code path through `evaluate_renew(license, requested_end=…)`
    means one set of policy checks (queue, embargo, max_renew_days, cap).
    No more day-rounding, no more "did the resolver round down to N
    days?" debugging.
  - Best UX: the portal calls `evaluate_renew`-equivalent logic locally
    to pre-validate (we can expose `GET /readium/v1/licenses/{id}/renewal-preview?requested_end=…`
    as a free-by-product later if needed) and shows the resolved
    `expires_at` in the confirmation dialog.

Implementation Plan updates:
  - Phase 1 A2 dropped; Phase 1 A2′ (replacement): `UpdateLicenseForm`
    accepts ONLY `requested_end` for the renew action. `duration`
    field removed from the form. Backwards-compat shim: if a payload
    carries `duration` (legacy clients), translate it to
    `requested_end = now + duration` and log a single deprecation
    line per request (no header — clients shouldn't rely on legacy).
  - Phase 1 A3 tests: drop the "renewed + duration" success case and
    add an "explicit requested_end accepted; computed from legacy
    duration also accepted but warned" pair.
  - `LicenseService.renew_license(license, new_duration_days=…)`
    signature: positional `new_duration_days` deprecated, callers
    pass `new_end_date=` only. One release window before removal.
  - `docs/readium/integration-contract.md` (F1) renew section:
    show `requested_end` only; mention `duration` deprecation in a
    "Migration notes" callout.
```

---

### Q2 🔴: Download token coupling to OPDS feeds — pre-mint in the feed or always require the explicit mint call?

**Issue**: The feed builder serializes `LicenseSerializer.Base.download_url`.
With Phase 4 D2, that URL includes a single-use token. A feed that ships
ten licenses thus mints ten tokens at feed-render time, each with a 60s
TTL. If the user fetches the feed and opens a link 30s later it works;
60s later it doesn't.

**Question**: How does the download URL behave in feeds?

**Options**:

- [X] **A**: Pre-mint per feed entry with longer TTL when feed-context
  is detected (e.g., 30 minutes). Trade: looser secrecy window for
  better UX.
- [ ] **B**: Don't pre-mint in feeds; emit a *mint URL* instead
  (`POST /readium/v1/licenses/{id}/download-tokens` returns the
  download URL). Frontend / reading app makes the call. Most secure.
- [ ] **C**: Pre-mint with the default 60s TTL; document the
  expectation that feeds are consumed immediately. Simplest.

**Answer**:

```
This make sense. Utilize nicely with redis TTL and generalize in terms of project. We should already have such layer.
Check auth and token scopes.
```

**Resolution**:

```
Option A chosen with scope expanded — pre-mint in OPDS feeds with a
longer TTL, but as part of a *generalized capability-token layer*
backed by the existing Django/Redis cache (`apps/api/views/tokens.py`
already uses `cache.set("refresh_token:{jti}", …, ttl)` against
`django.core.cache` — same pattern, new namespace).

Promote Phase 4 from "LicenseDownloadToken model" to
"`CapabilityTokenService` (reusable)":

  - Module: `apps/core/services/capability_tokens.py`.
  - API:
        CapabilityTokenService.mint(scope: str, subject: dict, ttl: int) -> str
        CapabilityTokenService.consume(token: str, expected_scope: str) -> dict | None
        CapabilityTokenService.peek(token: str, expected_scope: str) -> dict | None
    Backed by `django.core.cache` with key namespace
    `cap:{scope}:{token}`. `consume` is single-use (atomic `cache.delete`
    + return payload); `peek` is read-only (used for feed-served URLs
    that should be openable repeatedly within the window — see TTL
    table below).
  - Token format: URL-safe base64 of 32 random bytes (`secrets.token_urlsafe(32)`).
  - Auth/scope enforcement: at mint time, the caller resolves the
    object-permission via the existing `object_checker` (e.g.
    `check_license_download`). The minted payload includes
    `{"sub": user_id, "scope": "lcpl_download", "resource_id":
    license_id, "issued_at": ts}` and consumption verifies
    `scope` AND `resource_id` match the URL.

  Scopes introduced in this proposal:
    - `lcpl_download` — single-use, TTL 60s, scope-keyed to
      `license_id`. Issued by `POST /licenses/{id}/download-tokens`.
    - `lcpl_feed_download` — multi-use within TTL (peek, not consume),
      TTL 1800s (30 min), scope-keyed to `license_id`. Issued
      automatically when `LicenseSerializer.Base.download_url` is
      rendered inside an OPDS feed context.

  Settings:
    EVILFLOWERS_CAPABILITY_TOKEN_LCPL_TTL_SECONDS=60
    EVILFLOWERS_CAPABILITY_TOKEN_LCPL_FEED_TTL_SECONDS=1800

Implementation Plan updates:
  - Phase 4 D1 (was: model + migration) replaced with
    "`CapabilityTokenService` in `apps/core/services/` + scope
    registry. No DB model needed — Redis TTL handles eviction
    natively." Removes the migration, removes the hourly purge task
    (purge_used_download_tokens), removes the index/constraint on a
    non-existent table.
  - Phase 4 D2: `LicenseDownloadView.get` accepts `?token=…`, calls
    `CapabilityTokenService.consume(token, expected_scope="lcpl_download")`
    or `.peek(token, expected_scope="lcpl_feed_download")` based on
    a `mode` query param OR based on which scope the token carries.
  - Phase 4 D2: `LicenseSerializer.Base.download_url` mints
    `lcpl_feed_download` when serialized inside `_opds_feed=True`
    context, `lcpl_download` otherwise. The context flag is set by
    OPDS view code.
  - Phase 4 D3 (purge task) removed; Redis TTL evicts.
  - Phase 4 D4 tests reworked: single-use (lcpl_download) and
    multi-use (lcpl_feed_download); cross-scope rejection; expired
    rejection; cross-user mint rejection.
  - New test file: `apps/core/tests/test_capability_tokens.py`
    covering scope/TTL/consume vs peek/single-use semantics.

The generalization pays off later: Dataverse external-URL redirects
and any other capability-style flow can reuse the same service with a
new scope value.
```

---

### Q3 ⚠️: Bearer JWT on `.lcpl` download — Sunset date or accept indefinitely?

**Issue**: Phase 4 D5 deprecates `?access_token=…` for `.lcpl` in
favour of single-use tokens. A Sunset header lets us track real
removal; "accepted indefinitely" is the easy-but-soft alternative.

**Question**: Hard removal target or soft acceptance?

**Options**:

- [ ] **A**: Emit `Sunset: <date>` on Bearer-authenticated `.lcpl`
  responses; remove after 90 days (Recommended). Pushes elvira-portal
  to migrate; gives ops a date to plan against.
- [ ] **B**: Accept Bearer indefinitely as fallback for non-browser
  clients.
- [X] **C**: Drop Bearer support for `.lcpl` immediately when Phase
  4 ships.

**Answer**:

```
Force change on portal. Rather remove weird and dead code.
```

**Resolution**:

```
Option C chosen — hard cut, no feature flag. When Phase 4 ships,
`.lcpl` accepts the capability token ONLY. Bearer JWT via
`?access_token=…` (the path through `SecuredView._authenticate` at
`apps/core/views.py:25`) is disabled specifically for
`LicenseDownloadView` — the view stops deriving the user from
`SecuredView` and resolves the requester exclusively through the
capability token.

Concretely:
  - `LicenseDownloadView` no longer extends `SecuredView`; it
    extends `django.views.View` directly. The token is the auth.
  - `?access_token=…` on `.lcpl` returns 401 with a problem-detail
    body whose `detail_type` instructs clients to call
    `POST .../download-tokens` first. No Sunset header, no rollout
    flag, no soft period.
  - The general `SecuredView` `?access_token=…` URL-param path
    elsewhere is unaffected; this scope-narrow change applies to
    `.lcpl` only.

Deploy ordering is the only coordination mechanism:
  1. Portal lands G1 (`useDownloadLicense` → mint + open) on `dev`
     and verifies against a local EFC build that already token-only
     routes `.lcpl` (e.g. a branch checkout).
  2. EFC merges Phase 4 PR on `develop`. From that point on, Bearer
     is rejected on `.lcpl`.
  3. EFC deploys to dev → stage → prod with the portal already
     shipping G1. Any portal build that predates G1 is
     incompatible — operationally we coordinate the portal rollout
     to lead by one release.

Phase 7 G8 tracks the cross-repo deploy ordering. No EFC code path
checks any "require token" setting — the new behavior is the only
behavior.
```

---

### Q4 ℹ️: Beat schedule cadence for `expire_lapsed_licenses`

**Issue**: Phase 2 B1 sets the sweep at every 5 minutes. Operators may
prefer per-minute (lower borrow-rejection latency) or hourly (lighter).

**Question**: What cadence?

**Options**:

- [X] **A**: Every 5 minutes (Recommended). Bounded re-borrow latency,
  trivial cost.
- [ ] **B**: Every minute. Tighter SLA, higher write churn.
- [ ] **C**: Hourly + on-read fallback in `can_user_borrow` is enough.

**Answer**:

```
Cool to me
```

**Resolution**:

```
Option A confirmed — `expire_lapsed_licenses` runs on a 5-minute beat
schedule via Celery (`evil_flowers_catalog/celery.py`). No further
plan changes; the on-read fallback in `can_user_borrow`
(Phase 2 B2) covers the gap between sweeps.

Telemetry: emit a single INFO log per run with structured fields
`event=readium.expire_sweep transitioned=<N> errors=<N>` so
operators can grep `transitioned > 0` to spot a backlog.
```

---

### Q5 ⚠️: `LicenseAction` location — `License.LicenseAction` nested or top-level enum?

**Issue**: Phase 1 A1 introduces a sibling enum to `LicenseState`.
Nested keeps the namespace clean (`License.LicenseAction.RENEW`); top-
level (`apps/readium/enums.py::LicenseAction`) makes it importable
without `from apps.readium.models import License` (avoids circular
imports in forms / services).

**Question**: Where does the enum live?

**Options**:

- [X] **A**: Top-level `apps/readium/enums.py` (Recommended). Avoids
  circular-import pressure from forms / serializers / services.
- [ ] **B**: Nested `License.LicenseAction`. Matches `LicenseState`'s
  current location.
- [ ] **C**: Top-level, but also re-export from `License.LicenseAction`
  as a shim for backwards compatibility.

**Answer**:

```
Yeah - make sense.
```

**Resolution**:

```
Option A confirmed — `LicenseAction` lives at module level in
`apps/readium/enums.py::LicenseAction`. Form / serializer / service
import from there without dragging in `License` (which would create a
circular import with `apps/readium/models.py`).

The Technical Details code sample is updated:

    # apps/readium/enums.py (new)
    from django.db import models
    from django.utils.translation import gettext_lazy as _

    class LicenseAction(models.TextChoices):
        ACTIVATE = "active", _("Activate")
        RETURN = "returned", _("Return")
        RENEW = "renewed", _("Renew")
        REVOKE = "revoked", _("Revoke")
        CANCEL = "cancelled", _("Cancel")

    # apps/readium/models.py (UNCHANGED — LicenseState stays where it is)

Form import becomes `from apps.readium.enums import LicenseAction`.
No shim; first-class top-level only.
```

---

### Q6 ⚠️: `EVILFLOWERS_READIUM_MAX_RENEWALS` default — None or a number?

**Issue**: Phase 5 E2 introduces a renewal cap. STU's stated policy is
"two renewals max"; other deployments may not want a cap.

**Question**: What's the default?

**Options**:

- [X] **A**: Default `None` (no cap); STU sets `=2` in env
  (Recommended). Opt-in.
- [ ] **B**: Default `2` (matches STU policy); other deployments
  opt-out.
- [ ] **C**: No default — the setting is required in `settings/base.py`.

**Answer**:

```
Yep - looks cool.
```

**Resolution**:

```
Option A confirmed — `EVILFLOWERS_READIUM_MAX_RENEWALS` defaults to
`None` (no cap). STU sets `=2` in its environment per the existing
"two renewals max" policy. Other deployments are unconstrained out of
the box.

`evaluate_renew` short-circuits the cap check when the setting is
`None`:

    max_renewals = getattr(settings, "EVILFLOWERS_READIUM_MAX_RENEWALS", None)
    if max_renewals is not None and license.renewal_count >= max_renewals:
        return RenewDecision(False, reason=f"Renewal cap reached ({license.renewal_count}/{max_renewals})")

`LicenseSerializer.Base.renewals_remaining` returns `None` (not an
integer) when the cap is unset — surfaces "unlimited" to the
frontend without making it a magic number.
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-05-25 | Claude AI | Initial draft. Closes the integration-surface gaps surfaced in the 2026-05-25 review with the elvira-portal team after IP-008 landed: lifecycle vocabulary (Cluster A), expiry reconciliation (Cluster B), LSD read-after-write (Cluster C), single-use download tokens (Cluster D), renewal counter (Cluster E), integration-contract docs for elvira-portal (Cluster F). Review Questions Q1–Q6 added. |
| 2026-05-25 | jdubec | Resolved Review Questions Q1–Q6. Q1 (renew payload): canonicalize on `requested_end`; deprecate `duration` with a one-release legacy translation shim. Q2 (download tokens): promote to generalized `CapabilityTokenService` backed by the existing Redis cache; introduce two scopes (`lcpl_download` single-use 60s, `lcpl_feed_download` multi-use 30min); no DB model, no purge task. Q3 (Bearer on `.lcpl`): hard cut, no feature flag — `LicenseDownloadView` becomes token-only and stops extending `SecuredView`; cross-repo deploy ordering is the only coordination (portal G1 ships first, EFC Phase 4 second; tracked in Phase 7 G8). Q4 (beat cadence): 5-minute beat confirmed; structured log on every run. Q5 (LicenseAction location): top-level `apps/readium/enums.py`. Q6 (MAX_RENEWALS): default `None`; `renewals_remaining` exposed as `None` when cap unset. Implementation Plan updated to match — Phase 1 A2 rewritten as A2′, Phase 4 D1–D4 rewritten around `CapabilityTokenService`, Phase 4 D3 reframed as the hard-cut item, Phase 4 D5 references token-only auth. Added Phase 7 (G1–G8) tracking the eight elvira-portal GitHub issues Jakub will file against `EvilFlowersCatalog/elvira-portal` `dev` on proposal acceptance. Status flipped to ✅ Resolved. |
| 2026-05-25 | jdubec | Folded the Q1–Q6 resolutions into the rest of the proposal so the document reads consistently end-to-end. Updates: Key Components #4 + #7 rewritten around `CapabilityTokenService` and Phase 7 issue creation; Architecture mermaid replaces the `LicenseDownloadToken` node with two-scope `CapabilityTokenService` + a token-rejection edge for Bearer; API Changes table notes the hard cut on Bearer and the canonical `requested_end` payload; Alternatives 3 renamed (capability tokens, not "single-use download tokens") and new Alternative 5 documents why a DB model was rejected; Trade-offs section calls out the cross-repo deploy cost; Risks table swaps the "token store grows unboundedly" + Bearer Sunset rows for "Redis eviction" + "deploy ordering" rows; Success Criteria adds OPDS feed token, Bearer-401 case, `renewals_remaining=None` semantics, and Phase 7 G1–G8 issue creation; Future Considerations notes the eventual `duration` shim removal and additional `CapabilityTokenService` scopes; References point to `apps/api/views/tokens.py` as the existing Redis-cache pattern reused by `CapabilityTokenService`. |
