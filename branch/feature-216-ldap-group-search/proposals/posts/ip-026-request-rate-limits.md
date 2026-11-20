---
draft: true
date: 2026-11-09
authors:
  - jdubec
categories:
  - Feature
tags:
  - security
  - api
  - redis
---

# IP-026: Request Rate Limits

<!-- more -->

## Status

**Draft.** Implemented on the working tree with the recommended answers and tested locally (#21). **Off by
default** (`EVILFLOWERS_RATE_LIMIT_ENABLED=0`): turning it on, and the numbers, are a deployment decision
(EvilFlowersCatalog/knowledge#24). Awaiting review answers.

## Problem Statement

Nothing limits how often a client may call the catalog. Two places have their own limits: the assistant's daily
quota (IP-015) and the Readium hint page. Everything else is unlimited: REST, OPDS, Readium status documents,
`/data/v1` downloads and MCP. One misbehaving script or crawler can saturate the gunicorn workers, and password
guessing over HTTP Basic is unthrottled.

#21 (2022) asks for limits per hour, stored in Redis. Its 2023 maintainer comment set out the options:
- a reverse proxy, which can limit by IP only;
- the application, which can limit by user or API key and stays consistent across instances through Redis.

The comment also pointed to the W3C API's headers as the model.

## Proposed Solution

Application level, in `SecuredView.dispatch`. Every surface derives from it: REST, OPDS 1.2 and 2.0, Readium
status and licenses, `/data/v1` downloads and MCP. So one check covers them all, right after authentication, when
the client is known.

- **Algorithm.** A fixed window (`EVILFLOWERS_RATE_LIMIT_WINDOW`, default 3,600 s) per client. The count lives
  in the default cache (Redis): `cache.add` then `cache.incr`, which is atomic. Every worker and replica share it.
- **Who is the client:**
  - A signed-in user, by account: one budget per user across their web session and API keys. With
    `EVILFLOWERS_RATE_LIMIT_KEY=api_key`, each API key gets its own budget.
  - Anyone else, by IP address. The address is taken from `X-Forwarded-For` at the position the trusted proxies
    append to (`EVILFLOWERS_RATE_LIMIT_PROXY_COUNT`, default 1 for the k8s ingress). The leftmost entry is
    written by the client and can be forged, so it is never used.
- **Budgets:** `EVILFLOWERS_RATE_LIMIT_ANONYMOUS` (3,000 per window, per IP) and `_AUTHENTICATED` (5,000, per
  account or key).
- **Not counted:**
  - Superusers (`EVILFLOWERS_RATE_LIMIT_EXEMPT_SUPERUSERS`, default on), because bulk imports run through MCP
    as an administrator.
  - The continuation requests of a ranged download. A PDF viewer fetches a book in dozens of `Range` requests;
    the first one counts.
  - Views with `rate_limited = False`. None set it yet; worker callbacks such as the Readium hooks are not
    `SecuredView`.
- **Refused credentials** (a 401 from authentication) count against the IP's anonymous budget, so a password
  cannot be tried more than the anonymous budget allows per window.
- **Headers**, following the W3C API: `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`
  (epoch seconds) on every counted response. When the budget is used up the answer is **429** with problem type
  `/rate-limited`, the same headers, and `Retry-After`. `CORS_EXPOSE_HEADERS` lists them, so the portal can read
  them.
- **Failure mode:** when the cache is unreachable the request goes through and a warning is logged. A rate limit
  must not turn a Redis outage into a catalog outage.

## Implementation Plan

### Phase 1: Catalog ✅ on the working tree

- [x] `apps/core/ratelimit.py`: client identity, `client_ip`, fixed-window counter, `Decision.headers()`
- [x] `SecuredView.dispatch`: check after authentication; failed authentication counted per IP; headers on the
  response; `rate_limited` class switch
- [x] `DetailType.RATE_LIMITED`; settings; `CORS_EXPOSE_HEADERS`
- [x] Tests: `apps/core/tests/test_ratelimit.py` (budget and headers, forged `X-Forwarded-For`, per account vs per
  key, superusers, wrong passwords, ranged continuations, switch off, cache down)

### Phase 2: Deployment (knowledge#24)

- [ ] Measure a day of dev traffic per client first (Loki: requests per user and per IP per hour). Set the budgets
  well above the busiest legitimate client, then enable on dev.
- [ ] Check `EVILFLOWERS_RATE_LIMIT_PROXY_COUNT` against the real proxy chain (prod Compose: gateway nginx; dev:
  the ingress).
- [ ] Prod with the next release.

### Phase 3: Portal

- [ ] Show a readable message for 429 `/rate-limited`, using `Retry-After`.

## Technical Details

| File | Change |
|---|---|
| `apps/core/ratelimit.py` | new |
| `apps/core/views.py` | `rate_limited`, the check in `dispatch`, `_rate_limited()` |
| `apps/core/errors.py` | `DetailType.RATE_LIMITED` |
| `evil_flowers_catalog/settings/base.py` | `EVILFLOWERS_RATE_LIMIT_*`, `CORS_EXPOSE_HEADERS` |

A counter is one Redis key per client and window (`evilflowers:…:ratelimit:<client>:<window start>`), which
expires a minute after its window ends.

## Alternatives Considered

### Reverse proxy (nginx `limit_req`)

No code, but IP-only. It cannot give a signed-in reader their own budget behind a campus NAT, and it cannot tell
an API key from a browser. It remains useful as a coarse outer guard against floods.

### django-ratelimit

It is a decorator per view, and our views authenticate inside `dispatch`, after a decorator would have run. One
check in `SecuredView` covers every surface at once.

### Sliding window or token bucket

They are smoother at window edges, but need a sorted set or a Lua script per request. A fixed hourly window is
what #21 asked for, and its edge burst (up to twice the budget across a boundary) is acceptable at these budgets.

## Trade-offs and Risks

- **Campus NAT.** Anonymous readers behind one STU address share a budget. That is why the switch is off by default
  and the anonymous budget is generous; signed-in readers are unaffected.
- **Error responses** raised as exceptions (404, 403, …) are counted, but carry no `X-RateLimit-*` headers; only the
  429 itself and successful responses do.
- **One more Redis round trip** per counted request (`add` + `incr`).

## Success Criteria

- [ ] A day on dev with limits on and no 429 for a legitimate client (Loki: `|= "rate-limited"`)
- [ ] A scripted client hammering an endpoint gets 429 with `Retry-After`

## References

- EvilFlowersCatalog#21 (and its 2023 maintainer comment); W3C API rate limit headers
- IP-015 (assistant quota), EvilFlowersCatalog#8 (the other `SecuredView.dispatch` gate)

## Discussion

None yet.

## Review Questions

**Status**: ⏳ Awaiting Answers
**Review Date**: 2026-11-09
**Reviewer**: Claude AI

The following questions must be answered before implementation is considered final. Phase 1 applies the
recommended option of each.

---

### Q1: On by default? ⚠️ Medium

**Issue**: Implemented off. An anonymous budget is per IP, and readers behind the STU NAT share one.

**Question**: Should a fresh deployment have limits on?

**Options**:
- [ ] **A**: Off by default; each deployment turns it on after measuring its traffic (implemented, recommended)
- [ ] **B**: On by default with the current budgets

**Answer**:
```
```

**Resolution**:
```
```

---

### Q2: Superusers exempt? ℹ️ Low

**Issue**: Exempt by default, because bulk imports (`create_entry`, uploads, `classify_entries`) run through MCP as
an administrator and could need thousands of calls in an hour.

**Options**:
- [ ] **A**: Exempt, switchable (implemented, recommended)
- [ ] **B**: A separate, higher budget for superusers

**Answer**:
```
```

**Resolution**:
```
```

---

### Q3: The file views' IP block trusts the leftmost `X-Forwarded-For` ⚠️ Medium

**Issue**: `apps/files/views.py` `_get_client_ip` (used by the per-entry IP block, `evilflowers_ip_block`) takes
the **first** `X-Forwarded-For` entry. The client writes that entry, so anyone can claim an address inside the
allowed ranges. Rate limiting uses `ratelimit.client_ip()`, which reads the entry the trusted proxy appended.

**Question**: Switch the IP block to `ratelimit.client_ip()` too?

**Options**:
- [ ] **A**: Yes, in a separate change with its own test (recommended; it changes who may read IP-restricted books)
- [ ] **B**: Leave it

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
| 2026-11-09 | jdubec | Initial draft for #21; Phase 1 implemented and tested locally (Claude) |
| 2026-11-09 | jdubec | Added Review Questions section |
