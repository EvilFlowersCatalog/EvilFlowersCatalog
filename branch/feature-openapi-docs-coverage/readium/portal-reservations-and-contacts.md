# Portal Integration Spec — Reservations & Notification Contacts

**Status**: Backend deployed to dev (`develop`, 2026-08-18) — ready for FE implementation. **Updated 2026-09-03** after the August review round (see §8).
**Audience**: elvira-portal frontend developers
**Backend**: EvilFlowersCatalog (`https://dev.evilflowers.elvira.stuba.sk`)
**Scope**: Two end-to-end features: (1) reservation queue for fully-borrowed LCP publications, (2) self-service notification contacts (email delivery for all reservation/license notifications depends on them).

---

## Why this exists

Until now, borrowing a fully-borrowed book returned a generic `400 Validation error`, so the portal had no way to offer the queue — and users without a `NotificationContact` row silently received no emails. The backend now:

1. Returns **`409 CONFLICT` with a machine-readable `reason_code`** on borrow conflicts, carrying everything needed to render a "join the queue" UI.
2. Exposes **`/api/v1/notification-contacts`** so users can manage the email address notifications are delivered to.

---

## 1. API conventions

- **Auth**: standard Bearer JWT (same as all other portal calls).
- **Single-object envelope**: `{"response": { ... }}`
- **Paginated envelope**: `{"items": [ ... ], "metadata": {"page": 1, "limit": 10, "pages": 3, "total": 25}}`
- **Errors** (RFC-7807-style problem detail):

```json
{
  "title": "Cannot create license: No available slots for the requested period",
  "type": "/conflict",
  "detail": null,
  "additional_data": { "reason_code": "no_available_slots", "...": "..." }
}
```

---

## 2. Entry availability fields (already available)

Every entry serialization (`GET /api/v1/entries`, entry detail, entries nested in licenses/reservations) carries the LCP availability block. This is the primary driver of the borrow/reserve CTA:

| Field | Type | Meaning |
|---|---|---|
| `lcp_state` | enum | `not_lcp`, `available_now`, `available_in_days`, `active_loan_for_user`, `fully_borrowed` |
| `available_slots` | int | Free loan slots right now |
| `total_slots` | int | Configured concurrent-loan cap (`readium_amount`) |
| `active_count` | int | Currently active loans |
| `next_available_at` | datetime? | Earliest expiry among active loans (when no slot is free) |
| `user_active_license_id` | UUID? | Caller's active license, if any |
| `queue_length` | int | Users currently in the queue (`queued` + `available`) |
| `user_reservation_id` | UUID? | Caller's non-terminal reservation, if any |
| `user_position` | int? | Caller's queue position (only while `queued`; `1` = next in line) |

### CTA decision table (entry detail)

| Condition | CTA |
|---|---|
| `lcp_state == not_lcp` | No borrow UI (regular acquisition download) |
| `lcp_state == active_loan_for_user` | "Read / Manage loan" → license detail |
| `lcp_state == available_now` | **Borrow** |
| `lcp_state in (available_in_days, fully_borrowed)` and `user_reservation_id == null` | **Reserve** (show `queue_length`, and `next_available_at` as "expected from ~{date}") |
| `user_reservation_id != null` | "In queue — position {user_position}" + link to My reservations |

---

## 3. Borrow endpoint — new 409 contract

`POST /readium/v1/licenses`

```json
{ "entry_id": "<uuid>", "duration": "14 00:00:00" }
```

| Status | Meaning | FE action |
|---|---|---|
| `201` | License created (body: license incl. `download_url` for the `.lcpl`) | Success screen |
| `400` + `type: /passphrase-required` | User has no LCP passphrase; `additional_data.set_passphrase_url = "/api/v1/users/me"` | Prompt passphrase setup, then retry |
| `400` + `type: /validation-error` | Genuine validation problem | Show error |
| **`409`** + `type: /conflict` | **Borrow conflict — inspect `additional_data.reason_code`** | See below |

### `409` payloads

**`reason_code: "no_available_slots"`** — offer the queue:

```json
{
  "title": "Cannot create license: No available slots for the requested period",
  "type": "/conflict",
  "additional_data": {
    "reason_code": "no_available_slots",
    "entry_id": "3f7a…",
    "queue_length": 3,
    "next_available_at": "2026-08-29T10:00:00+00:00",
    "user_reservation_id": null,
    "reservations_url": "/readium/v1/reservations"
  }
}
```

**`reason_code: "not_lendable"`** (`409`) — the title is LCP-enabled but cannot be issued (no PDF/EPUB file, not encrypted yet, not registered with the LCP server). Nothing the reader can do; show `title` verbatim — it is already a reader-facing sentence — and offer the contact address:

```json
{
  "title": "This publication is not available for borrowing yet. Please contact the library.",
  "type": "/conflict",
  "detail": "Entry has no EPUB or PDF acquisition suitable for LCP protection",
  "additional_data": { "reason_code": "not_lendable", "entry_id": "3f7a…", "contact_email": "elvira@stuba.sk" }
}
```

`detail` is the technical cause for staff/logs — do not show it to readers (this is the "Szepesi Lapok" message from the August review).

**`reason_code: "already_borrowed"`** — point at the existing loan:

```json
{
  "additional_data": {
    "reason_code": "already_borrowed",
    "entry_id": "3f7a…",
    "existing_license_id": "9c1e…"
  }
}
```

If `user_reservation_id` is non-null in the `no_available_slots` payload, the user already queued (race with another tab) — navigate to My reservations instead of re-posting.

---

## 4. Reservations API

### Reservation object

```json
{
  "id": "…", "entry_id": "…", "user_id": "…",
  "position": 2,
  "status": "queued",
  "requested_at": "…",
  "available_at": null,
  "claim_deadline": null,
  "claimed_license_id": null,
  "created_at": "…", "updated_at": "…"
}
```

State machine (server-driven transitions marked ⚙):

```
queued ──⚙ promote──▶ available ──PATCH claimed──▶ claimed   (terminal)
   │                      │
   │                      └──⚙ deadline passes──▶ expired    (terminal)
   └────────── PATCH cancelled (from queued or available) ──▶ cancelled (terminal)
```

- `position` reflows automatically — position `1` is always next in line.
- When promoted to `available`, `available_at` and `claim_deadline` are set (claim window: **48 h** on dev) and the user gets the `reservation_available` email.
- An unclaimed `available` reservation expires via a per-minute sweep and the next user is promoted.

### Endpoints

| Endpoint | Notes |
|---|---|
| `POST /readium/v1/reservations` body `{"entry_id": "<uuid>"}` | `201` reservation. `409 /conflict` with `additional_data.reason_code` (see table below). Show the server's `title` as the message; branch on `reason_code`. |
| `GET /readium/v1/reservations?status=queued,available` | Paginated, **caller's own rows only** (default `scope=own`, also for admins), `Detailed` shape (embeds full `entry` incl. LCP fields — no extra fetch needed). Filters: `entry_id`, `status` (comma = OR), `scope`. |
| `GET /readium/v1/reservations?scope=managed` | Admin queue view: own rows **plus** reservations on entries in catalogs the caller manages; superusers get every reservation. Use this (and only this) for the staff "Reservations" tab — never for "My reservations" (GitHub #73). |
| `GET /readium/v1/reservations/{id}` | Single reservation — owner, superuser, or manager of the entry's catalog. |
| `PATCH /readium/v1/reservations/{id}` body `{"status": "cancelled"}` | `200`. Owner or superuser. `409` if already terminal. |
| `PATCH /readium/v1/reservations/{id}` body `{"status": "claimed"}` | **Owner only.** **`201` with the created License** (and `Location: /readium/v1/licenses/{id}`). `409` if not `available` / deadline passed. `400 /passphrase-required` as on borrow. |

### Reservation `409` reason codes (`additional_data.reason_code`)

| `reason_code` | When | FE action |
|---|---|---|
| `slots_available` | Entry has free capacity — reserving would strand the reservation | Offer **Borrow** instead (refresh entry state; a slot freed since the page loaded) |
| `already_borrowed` | Caller holds an active license on this entry | Point to the loan |
| `already_reserved` | Caller already queued/available on this entry | Navigate to My reservations |
| `reservation_cap_reached` | Caller has 5 non-terminal reservations | Explain the cap, link My reservations |

PATCH conflicts (`cancelled`/`claimed` on a terminal or expired-window reservation) also carry a `reason_code` when the underlying error is typed; treat a missing `reason_code` as a generic conflict and display `title`.

---

## 5. FE flows to implement

### Flow A — Reserve from entry detail (E2E)

1. User taps **Borrow** on an entry (or the CTA already says **Reserve** per the decision table).
2. On borrow `409 reason_code == "no_available_slots"` → show queue modal: "All {total_slots} copies are on loan. {queue_length} waiting, next return expected ~{next_available_at}. Join the queue?"
3. Confirm → `POST /readium/v1/reservations` → success toast "You are #{position} in line. We'll email you when it's your turn." → refresh entry state.
4. **Contact nudge**: after joining, `GET /api/v1/notification-contacts`; if the user has no primary email contact, show a banner: "Add an email address so we can notify you when the book is available" → deep-link to contact settings (Flow D).

### Flow B — My reservations page

- List `status=queued,available` (the `Detailed` rows embed the entry for title/author rendering).
- **`queued` row**: position badge, "expected from ~{entry.next_available_at}", **Cancel** (confirm dialog → PATCH `cancelled`).
- **`available` row**: highlighted, countdown to `claim_deadline`, **Borrow now** (PATCH `claimed`) and **Cancel**.
- Claim success (`201`) → route to the returned license (reader / `.lcpl` `download_url`).
- Claim `409` (deadline raced the sweep) → show "The claim window expired" and refresh the list.
- Claim `400 /passphrase-required` → passphrase setup flow, then retry the PATCH.
- Optionally show a collapsed history section (`status=claimed,expired,cancelled`).

### Flow C — Email claim deep link

The `reservation_available` / `reservation_claim_reminder` emails link to the catalog, which **302-redirects to the portal**:

```
{PORTAL_URL}/library/reservations/{reservation_id}/claim?access_token=<scoped-jwt>
```

Implement this route:

1. Ignore `access_token` (it is a scoped token for the catalog's fallback page, **not** a portal Bearer credential).
2. If no session → normal login, preserving the return URL.
3. `GET /readium/v1/reservations/{id}` → render a confirmation screen (entry title/author, deadline).
4. Confirm → PATCH `{"status": "claimed"}` with the user's Bearer token → same handling as Flow B.
5. `404`/`403` → "This reservation isn't available" fallback; `409` → expired-window message.

### Flow D — Notification contact settings (profile)

`/api/v1/notification-contacts`:

| Endpoint | Notes |
|---|---|
| `GET /api/v1/notification-contacts` | Paginated list: `{id, type, value, is_primary, created_at, updated_at}`. Currently `type` is always `"email"`. |
| `POST /api/v1/notification-contacts` body `{"type": "email", "value": "user@stuba.sk", "is_primary": true}` | `201`. Upsert semantics: same value updates in place; saving a primary **demotes any previous primary** of the same type. `400 /validation-error` on malformed email. |
| `DELETE /api/v1/notification-contacts/{id}` | `200`; `404` if not the caller's contact. |

UI:

- Profile section "Notifications": list addresses, primary marked, add-address form (client-side email validation mirrored server-side), set-primary action (re-`POST` with `is_primary: true`), delete with confirm.
- Empty state: "No email address — you won't receive reservation or loan notifications."
- Note for copy: LDAP (AIS) logins also auto-fill the address from the directory on each login; manual entries here are for overrides and database-auth users. Deleting the LDAP-sourced address is possible but it reappears on next login.

### Emails the user will receive (for consistent in-app copy)

| Type | Trigger |
|---|---|
| `reservation_placed` | Joined the queue (includes position + ETA range) |
| `reservation_available` | Promoted — claim link, 48 h deadline |
| `reservation_claim_reminder` | 6 h before the claim deadline |
| `reservation_expired` | Claim window missed |
| `reservation_cancelled` | Reservation cancelled |
| `license_created` / `license_returned` / `license_revoked` / `license_expiring_soon` | Loan lifecycle |

---

## 6. E2E test plan (Playwright, against dev)

Preconditions: a readium-enabled entry with `readium_amount = 1`; two test users (A, B) with passphrases set.

1. **Reserve when full**: A borrows (201). B opens the entry → CTA is *Reserve* (`lcp_state: fully_borrowed`). B borrows anyway via API → assert `409` + `reason_code: no_available_slots`. B reserves → position 1 visible on entry and in My reservations.
2. **Duplicate guards**: B reserves again → `409` `reason_code: already_reserved`; B's entry detail shows "in queue", not the Reserve CTA. Reserving an entry with a free slot → `409` `reason_code: slots_available`.
3. **Promotion**: A returns the loan → within ~1 min B's reservation flips to `available` with a `claim_deadline`; `reservation_available` email delivered (dev SMTP).
4. **Claim happy path**: B claims from My reservations → `201` license → `.lcpl` downloadable; reservation shows `claimed`.
5. **Claim via email link**: repeat 3, follow the email link → portal claim route → confirm → license created.
6. **Cancel + reflow**: with users B and C queued (positions 1, 2), B cancels → C's position becomes 1.
7. **Expiry**: (config-dependent — needs a short `EVILFLOWERS_READIUM_RESERVATION_CLAIM_HOURS` on a test env, or DB fixture) let the claim window lapse → reservation `expired`, next user promoted.
8. **Contacts**: add invalid email → inline validation error; add valid → appears as primary; add second as primary → first demoted; delete → empty-state banner returns; reservation flow shows the "add an email" nudge only when no contact exists.
9. **Passphrase gate**: user without passphrase claims → `400 /passphrase-required` → passphrase flow → retry succeeds.

Server timing to account for in tests: promotion on return is immediate; expiry/promotion sweeps run every minute; claim reminder sweep every 15 minutes.

---

## 7. Edge cases & rules

- **Race on join**: `POST /reservations` can 409 even after a 409-borrow suggested reserving (someone else took the last action) — always render the server message and re-fetch entry state.
- **Position changes**: positions shrink as people ahead cancel/claim. Refresh on page focus; no push channel exists. (Position-change emails exist behind a server flag, off by default.)
- **`available` reservations hold a virtual slot** — an entry can show `available_slots: 0` with no active loan for the viewer while someone's claim window is open. The CTA table above handles this correctly via `lcp_state`.
- **Anonymous users**: LCP fields serialize with defaults; hide borrow/reserve CTAs behind login.
- **Caching**: reservation list responses can come back `304` — ensure the HTTP client treats that as fresh-enough or bypasses cache after mutations.

---

## 8. August 2026 review round — what changed on the backend (2026-09-03)

| Finding (review doc / #73) | Backend change | FE action |
|---|---|---|
| "I could reserve the same book twice" — two rows of the same title under *Reserved* | The list leaked other users' reservations to catalog managers/superusers. `GET /reservations` now defaults to `scope=own`; the admin view is `scope=managed`. | "My reservations" needs no change (drop any client-side `user_id` workaround if present). Staff "Reservations" tab must add `?scope=managed`. |
| E-mail links point at `localhost` / the `.lcpl` button 401s | `EVILFLOWERS_BASE_URL` falls back to the public readium base URL; the `license_created` button now carries a 72 h capability token (`?token=`) the gateway accepts. Startup warning `readium.W001` fires when links would be loopback. | None. Set `EVILFLOWERS_PORTAL_URL` on the deployment so claim links land in the portal instead of the catalog's fallback page. |
| Claimed / borrowed book "not in my loans", admin sees it as *Pripravené* | `ready` **is a live loan** (issued, not yet opened in a reader; LSD flips it to `active` on first device registration — it never flips by itself). New `is_active_loan` on every License and `GET /licenses?active=true`. | *Borrowed* list: filter on `is_active_loan` (or request `?active=true`), never on `state == "active"`. Show "not opened yet" for `state == "ready"` if useful; there is nothing to "activate". |
| Unintelligible borrow error ("Entry has no EPUB or PDF acquisition…") | `409` + `reason_code: not_lendable`, reader-facing `title`, technical `detail`, `contact_email`. | Render `title`; offer `contact_email`. |
| Print allowance | `EVILFLOWERS_READIUM_PRINT_LIMIT_PAGES` / `EVILFLOWERS_READIUM_COPY_LIMIT_CHARS` (defaults 10 pages / 2048 chars) and per-entry `readium_print_limit` / `readium_copy_limit` config. LCP counts pages, not percentages — Thorium's print dialog still lists every page; the limit is enforced when printing. | None. |
| E-mail wording | Header/footer use `EVILFLOWERS_NOTIFICATION_LIBRARY_NAME`; dates humanised; "New e-book loan"; "do not reply" footer. Full texts: `docs/notifications/email-notifications.md`. | Keep in-app copy aligned. |
| No e-mail when the copy was freed | Not reproducible from code — promotion fired (the row was `available` with a 39 h countdown). Check `NotificationLog` for `reservation_available` rows with `status=failed` / `no_contact` for that user. | — |
