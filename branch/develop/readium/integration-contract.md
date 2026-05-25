# Readium Integration Contract

This document is the single source of truth for clients integrating
with the EvilFlowers Readium surface (license CRUD, LSD proxy, OPDS
acquisition links, `.lcpl` download). It is the contract IP-009
Phase 6 publishes so frontend teams (elvira-portal and any future
reader) can build against a stable shape.

When something here disagrees with the code, the code wins — file an
issue and update this page.

## Authentication

Bearer JWT (`Authorization: Bearer <token>`) authenticates **all
Readium endpoints except** the `.lcpl` download.

`.lcpl` download (`GET /readium/v1/licenses/{id}.lcpl`) is
**capability-token only** as of IP-009 Phase 4. Clients never embed
the bearer JWT in this URL. Instead, the License response (or any
OPDS feed entry that references the license) carries an absolute
`download_url` with a freshly-minted single-use token. Open that URL
directly.

| Endpoint | Auth |
|---|---|
| `POST /readium/v1/licenses` | Bearer |
| `GET /readium/v1/licenses` | Bearer |
| `GET /readium/v1/licenses/{id}` | Bearer |
| `PUT /readium/v1/licenses/{id}` | Bearer |
| `POST /readium/v1/licenses/{id}/renewals` | Bearer (or LSD callback) |
| `GET /readium/v1/licenses/{id}.lcpl?token=…` | **capability token only** |
| `PUT /readium/v1/licenses/{id}/return` | Bearer (LSD proxy) |
| `PUT /readium/v1/licenses/{id}/renew` | Bearer (LSD proxy) |
| `GET /readium/v1/licenses/{id}/status` | Bearer (LSD proxy) |
| `POST /readium/v1/reservations` | Bearer |
| `PATCH /readium/v1/reservations/{id}` | Bearer |
| `GET /readium/v1/entries/{id}/availability` | Bearer |

## License lifecycle vocabulary (IP-009 Phase 1)

The state machine has two enums that should not be conflated.

- `LicenseState` — the *resting* state the row is in. One of
  `ready / active / returned / expired / revoked / cancelled`.
  Only the server transitions these; clients read them.
- `LicenseAction` — the *verb* a client can ask the server to
  perform via `PUT /licenses/{id}`. One of
  `active / returned / renewed / revoked / cancelled`.

A successful `RENEW` action leaves the license in state `active`,
not `renewed`. There is no `renewed` resting state.

### `PUT /readium/v1/licenses/{id}` body

```json
{
  "action": "renewed",
  "requested_end": "2026-07-01T12:00:00Z"
}
```

Action × required body fields × resulting state:

| Action | Body | Resulting state |
|---|---|---|
| `active` | – | `active` (READY → ACTIVE, increments `device_count`) |
| `returned` | – | `returned` |
| `renewed` | `requested_end` (ISO-8601 datetime) | `active` (`expires_at` ← `requested_end`); `renewal_count` += 1 |
| `revoked` | – | `revoked` |
| `cancelled` | – | `cancelled` |

#### Legacy fields (deprecated, one-release shim)

- `state` — pre-IP-009 used the state value as the action. Still
  accepted; emits a `license_update_legacy_state_field` log line.
- `duration` (ISO-8601 duration like `P14D`) — pre-IP-009 the
  renew action used `duration`. Translated server-side to
  `requested_end = now + duration` and a deprecation log line is
  emitted.

Both will be removed one release after IP-009 Phase 1 ships.

### Renewal sub-resource (LSD callback)

```
POST /readium/v1/licenses/{id}/renewals
Body: { "requested_end": "<iso-8601>" }
```

This is the endpoint the Status Server's `renew_custom_url`
callback targets. Same payload shape as the PUT path; routes
through `evaluate_renew` policy. The portal UI can call either,
but `PUT /licenses/{id}` is the canonical user-driven entry point.

## Borrow → download flow

```
1. POST   /readium/v1/licenses        { entry_id, duration }
   →  201 { id, state="ready", ..., download_url, renewal_count, renewals_remaining }
2. Open `download_url` (carries `?token=<single-use, 60s>`)
   →  GET  /readium/v1/licenses/{id}.lcpl?token=…
   →  200 application/vnd.readium.lcp.license.v1.0+json (the .lcpl)
```

No second round-trip is needed — `download_url` is minted inline at
serialization time. Re-fetch the License response to obtain a fresh
URL if the user took longer than 60 seconds to act.

## Return flow

| What the client wants | How |
|---|---|
| Portal UI return button | `PUT /readium/v1/licenses/{id}` with `{action: "returned"}` |
| Reader app return (LSD spec) | `PUT /readium/v1/licenses/{id}/return` (LSD proxy) |

**There is no `PATCH` variant** on either endpoint. PATCH was never
in the Readium surface — clients that PATCH receive 405.

## Renewal flow

| What the client wants | How |
|---|---|
| Portal UI renew button | `PUT /readium/v1/licenses/{id}` with `{action: "renewed", requested_end}` |
| Reader app renew (LSD spec, `renew_custom_url`) | `POST /readium/v1/licenses/{id}/renewals` with `{requested_end}` |
| Reader app renew (LSD proxy passthrough) | `PUT /readium/v1/licenses/{id}/renew?end=…` |

`evaluate_renew` denies (403 + RFC 7807) when:

- The license is in a terminal state (`returned / expired / revoked / cancelled`).
- `renewal_count >= EVILFLOWERS_READIUM_MAX_RENEWALS` (when configured; default uncapped).
- A reservation queue exists on the entry.
- The license is inside the post-acquisition embargo (`EVILFLOWERS_READIUM_RENEW_EMBARGO_DAYS`).
- `requested_end` exceeds `EVILFLOWERS_READIUM_MAX_RENEW_DAYS` from now.

## Entry pagination contract — LCP-aware fields

Every entry list (`GET /api/v1/catalogs/{c}/entries`) and detail
(`GET /api/v1/entries/{id}`) response carries the following fields,
populated per-user by `lcp_state_mapping`. Clients should read them
instead of making extra `/availability` calls.

| Field | Type | Use for |
|---|---|---|
| `lcp_state` | enum | One of `not_lcp / available_now / available_in_days / active_loan_for_user / fully_borrowed`. Drives the badge/button state. |
| `available_slots` | int | Concurrent licenses still available. |
| `total_slots` | int | `readium_amount` cap on this entry. |
| `active_count` | int | Active licenses right now. |
| `over_saturated` | bool | True when `active_count > total_slots` (legacy data only). |
| `next_available_at` | datetime/null | When a slot is expected to free up; `null` if `available_slots > 0`. |
| `user_active_license_id` | UUID/null | The requester's own active license, if any. |
| `queue_length` | int | Reservations queued for this entry. |
| `user_reservation_id` | UUID/null | The requester's own reservation, if any. |
| `user_position` | int/null | The requester's queue position. |

`/readium/v1/entries/{id}/availability` is still available for the
calendar widget that needs per-day breakdown; everything else should
read the inlined fields.

## License response shape

```json
{
  "id": "…",
  "entry_id": "…",
  "user_id": "…",
  "state": "ready",
  "starts_at": "2026-05-25T10:00:00Z",
  "expires_at": "2026-06-08T10:00:00Z",
  "renewal_count": 0,
  "renewals_remaining": null,
  "download_url": "https://…/readium/v1/licenses/…/?token=…",
  "reader_requirements": { … },
  "created_at": "…",
  "updated_at": "…"
}
```

`renewals_remaining` is `null` when `EVILFLOWERS_READIUM_MAX_RENEWALS`
is unset (uncapped). When set to an integer (e.g. STU's `2`), it
returns `max(0, cap - renewal_count)`.

## Reservation flow

```
POST   /readium/v1/reservations          { entry_id }
PATCH  /readium/v1/reservations/{id}     { status: "cancelled" | "claimed" }
```

`PATCH` here is the **only** PATCH method in the Readium surface.

## Errors (RFC 7807 problem details)

Standard error shape from `apps.core.errors.ProblemDetailException`:

```json
{
  "title": "…",
  "type": "/forbidden",
  "detail": "…",
  "additional_data": { … }
}
```

Common `detail_type` values used by Readium endpoints:

- `/passphrase-required` — License create when the user has no
  default `lcp_passphrase_hash` and didn't supply one. The
  `additional_data` contains `set_passphrase_url`.
- `/conflict` — Renewal denial; reservation transitions; oversaturated
  entry.
- `/forbidden` — License revoked / expired; capability-token failures.
- `/validation-error` — Form failures (missing fields, invalid
  enums, malformed dates).
- `/not-found` — License / entry / reservation missing.

## Capability tokens (`.lcpl` only)

| Scope | Mode | TTL (default) | Where it appears |
|---|---|---|---|
| `lcpl_download` | single-use (consume) | 60s | `download_url` on any `License` response when no OPDS feed context |
| `lcpl_feed_download` | multi-use (peek) | 1800s | `download_url` inside OPDS feed entries |

Configuration:

```sh
EVILFLOWERS_CAPABILITY_TOKEN_LCPL_TTL_SECONDS=60
EVILFLOWERS_CAPABILITY_TOKEN_LCPL_FEED_TTL_SECONDS=1800
```

Tokens are stored in Redis (the `django.core.cache` backend). Server
restart loses unredeemed tokens — clients should always re-fetch the
License (cheap) before opening the download URL after a long idle.

A token's `resource_id` is bound to the license `id`; replaying a
token against a different license URL → 401.

## Related proposals

- [IP-008](../proposals/posts/ip-008-readium-correctness-followup.md) — the structural
  rewrite (atomic borrow, LSD compliance, OPDS LCP link parity) this
  contract builds on.
- [IP-009](../proposals/posts/ip-009-readium-integration-closeout.md) — the integration-closeout
  proposal that introduced this document.
- [elvira-portal action items](elvira-portal-action-items.md) — the frontend-side
  punch-list paired with IP-009.
