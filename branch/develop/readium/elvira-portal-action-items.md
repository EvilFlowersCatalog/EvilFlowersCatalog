# elvira-portal Action Items (IP-009 Phase 7)

Companion to the [Readium integration contract](integration-contract.md).
The list below is the source of truth for the GitHub issues filed
against [`EvilFlowersCatalog/elvira-portal`](https://github.com/EvilFlowersCatalog/elvira-portal)
on the `dev` branch when IP-009 lands. Every issue references this
document and the corresponding clusters from the proposal.

Each task here maps one-to-one with a `G*` checkbox in IP-009
Phase 7.

## G1 — Switch `.lcpl` download to capability-token flow

**Why**: `useDownloadLicense.tsx` currently builds
`…/readium/v1/licenses/{id}.lcpl?access_token=${auth.token}` and
opens it in `thorium://`. The long-lived bearer JWT ends up in
browser history, referrer headers, and reverse-proxy access logs.
After IP-009 Phase 4 the EFC backend rejects bearer auth on `.lcpl`
with 401.

**Required changes**:

1. Remove all JWT concatenation from `useDownloadLicense.tsx`.
2. Read `download_url` from the existing License response (already
   minted with a capability token by the backend serializer — no
   separate mint call needed).
3. Hand `download_url` to the `thorium://` link / new-tab opener.

**Acceptance**:

- Network panel shows no `?access_token=` parameter on any
  `.lcpl`-bound URL.
- Smoke-test on dev: download works against an EFC build that
  carries IP-009 Phase 4.

## G2 — Stop concatenating `lcp_license_id` into download URLs

**Why**: `LoansTable.tsx` has
`const licenseId = item.lcp_license_id || item.id; openInThorium(licenseId);`
The backend route resolves the **local** license PK (`item.id`),
not the LCP-server UUID. When `lcp_license_id` is populated
(post-issuance), the URL 404s.

**Required changes**: always use `item.id`. Better: consume the
`download_url` field minted by the backend serializer and stop
building URLs client-side entirely.

**Acceptance**: an existing loan with non-null `lcp_license_id`
downloads on first click without a 404.

## G3 — Update `LICENSE_STATE` enum and add `LICENSE_ACTION`

**Why**: `src/utils/interfaces/licenses.ts` has the value `draft`
(typo for `ready`) and no `renewed` value. `AdminLoans.tsx` uses a
hardcoded list that does not match the enum.

**Required changes**:

1. Fix the typo: `draft` → `ready`.
2. Add a `LICENSE_ACTION` enum mirroring backend
   `LicenseAction`: `active / returned / renewed / revoked /
   cancelled`. Use this enum for verbs in the renew/return UI.
3. `useUpdateLicense` posts `{action: …}` (not `{state: …}`).

**Acceptance**: typecheck passes; clicking "Renew" sends `action:
"renewed"` and the backend returns 200.

## G4 — Renew payload uses `requested_end`, not `duration`

**Why**: IP-009 Q1 canonicalized the renew payload on
`requested_end`. `duration` is a one-release legacy shim.

**Required changes**:

1. Replace `'P1Y'`-style ISO durations in `AdminLoans.tsx` with an
   ISO-8601 datetime, picked via a date picker bound to
   `evaluate_renew`'s window.
2. PUT body becomes `{action: "renewed", requested_end:
   "<iso-8601>"}`.

**Acceptance**: PUT returns 200; no `duration` payload sent from
any code path.

## G5 — Use HTTP PUT (not PATCH) for license state transitions

**Why**: The 2026-05-25 meeting captured a 405 on PATCH return.
The dev branch already uses PUT in `useEditLicense.tsx` /
`useUpdateLicense.tsx`, but older deployed bundles may not.

**Required changes**:

1. Audit the codebase for any `axios.patch("/readium/v1/…")`.
2. Redeploy dev/stage so Tomáš's traced 405 disappears.

**Acceptance**: returning an active loan succeeds; backend access
logs show PUT.

## G6 — Consume inlined Entry LCP fields; drop redundant `/availability` calls

**Why**: The backend already inlines `lcp_state`, `available_slots`,
`total_slots`, `active_count`, `over_saturated`, `next_available_at`,
`user_active_license_id`, `queue_length`, `user_reservation_id`,
`user_position` on every entry list/detail response. The portal
calls `/readium/v1/entries/{id}/availability` to derive the same
data.

**Required changes**:

1. Extend the TS `IEntry` type with the inlined fields.
2. In entry-detail and entry-card components, render
   "available / full / borrowed by you / queue: N (you are #M)"
   from the inlined fields.
3. Keep `/availability` only for the calendar widget that needs
   per-day data.

**Acceptance**: entry-detail page reads loan/queue state from a
single round-trip; network panel shows no `/availability` call on
simple browse.

## G7 — Surface renewal counter and remaining renewals

**Why**: IP-009 Phase 5 exposes `renewal_count` and
`renewals_remaining` on `LicenseSerializer.Base`. The portal should
display "Renewed X times" + "X renewals remaining (cap: N)" on the
loan card / detail.

**Required changes**:

1. Extend TS `ILicense` type with `renewal_count: number` and
   `renewals_remaining: number | null`.
2. Render the count + remaining.
3. Disable the "Renew" button when `renewals_remaining === 0`.

**Acceptance**: loan detail shows renewal history; renew button
greys out at cap.

## G8 — Cross-repo deploy ordering

**Why**: IP-009 Phase 4 is a hard cut on bearer auth for `.lcpl`.
There is no EFC feature flag — the only coordination mechanism is
deploy ordering. Portal G1 must ship and be deployed to the
environment **before** the EFC Phase 4 PR merges.

**Required changes**:

1. Land G1 on `dev` and deploy to the dev environment.
2. Coordinate with the EFC backend team: backend merges Phase 4 PR
   after portal G1 is live on the same environment.
3. Repeat for stage → prod.

**Acceptance**: production access logs show zero `.lcpl` requests
with bearer auth in the 7 days following the EFC deploy.

## Reference

- Backend [integration contract](integration-contract.md)
- Proposal [IP-009](../proposals/posts/ip-009-readium-integration-closeout.md)
