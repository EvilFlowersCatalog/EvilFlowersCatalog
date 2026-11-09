---
draft: true
date: 2026-11-09
authors:
  - jdubec
categories:
  - Feature
tags:
  - api
  - nginx
  - caching
---

# IP-027: One HTTP Cache Policy, From Django to the Edge nginx

<!-- more -->

## Status

**Draft.** The catalog part (Phase 1) is implemented on the working tree with the recommended answers and
tested locally. The nginx part (Phase 2) is a deployment change in the knowledge repository. Awaiting review
answers.

## Problem Statement

#123: after a loan is returned on `/loans`, the book stays under "Borrowed" until a reload. The issue suspects
HTTP caching. It names a missing `Cache-Control`, a `Vary` without `Authorization`, and a possible nginx/Envoy
cache. A wider look shows the caching *is* undefined almost everywhere, but **it is not what broke #123**.

### What #123 actually was

The dev access log for the session in the issue (Loki, `vhost="dev.evilflowers.elvira.stuba.sk"`,
2026-11-09 22:12) shows the following:

- Every list request reached Django and got a full `200` (≈35 KB). No `304`, and dev has no proxy cache, so no
  cache served a stale list.
- The license list takes **2.6–3.7 s** for one reader with 50 loans (`rt=3.177`, `rt=3.494`, …).
- The return `PUT` started at 22:12:50.05 and answered at 50.47. Two list `GET`s that **started before it**
  (≈47.2 s) answered **after** it (50.47 and 50.72). By timing they must carry the pre-return state; only
  body sizes are logged, so the states are inferred.
- Two refetches issued right after the `PUT` were aborted by the browser (`499`, `rt=0.509`). Another one
  started at ≈52.06 and returned `200` at 22:12:55, with a different body (34,652 bytes against 34,894 before).
  The fresh list did arrive, about **5 s** after the return. That matches the issue's "only updates later".
- The server side was consistent: no `lsd.reconcile.fallback`, and the `license_returned` notification went out
  at 22:12:50.4, so the row said `returned` when the `PUT` answered.

So two things combined:

- **A slow endpoint, in this repository.** Each return triggers two ≈3.3 s list reads, and the screen can only
  be right once a read that started after the `PUT` finishes.
- **A client-side ordering race.** Responses to reads that started before the return land after it, and show
  the old state in the meantime.

Headers fix neither. Both are tracked in Review Question Q1. A regression test now pins the server half: a list
read after the `PUT` reports the terminal state and is never a `304` against the earlier copy.

### What is undefined everywhere

| Surface | Today | Risk |
|---|---|---|
| Most REST/Readium/MCP JSON | `ETag` from `ConditionalGetMiddleware`, no `Cache-Control`, `Vary` without `Authorization` | Each cache guesses. A shared cache may hand one user's list to another. |
| Writes and errors | no `Cache-Control` | Nothing says "never store". |
| Covers and thumbnails (`/data/v1/covers`, `/thumbnails`) | `public, max-age=86400`, no validator | A replaced cover shows the old image for up to a day, then is downloaded again in full. |
| Prod nginx `/data/v1/` | `proxy_cache` with `proxy_cache_valid 200 201 303 4w`. The key is the URL only. | Any header-less `200`/`303` under `/data/v1/` is kept for **four weeks** and served to **every** caller of that URL. That includes the acquisition download's `303` to the **signed-in reader's own copy** (`SeeOtherResponse`, no `Cache-Control`) and `format=base64` downloads. A second reader of the same acquisition can be redirected to the first reader's personal copy. **Live on prod today.** |
| Portal `index.html` (all SPA vhosts) | `Last-Modified`, no `Cache-Control` | Browsers apply heuristic freshness (≈10% of the file's age), so readers keep the previous release's shell after a deploy. |
| Hashed Vite chunks (`/assets/*-<hash>.js`) | no `Cache-Control` | Revalidated or heuristically cached, although they can never change. |

## Proposed Solution

One rule: **every response states its policy, and the default is the safe one.**

### Catalog (Django)

1. **`CacheControlMiddleware`** (`apps/api/middleware/cache_control.py`), right below
   `ConditionalGetMiddleware`, only touches responses that set no `Cache-Control`:
    - unsafe methods (`POST`, `PUT`, `PATCH`, `DELETE`) and status ≥ 400 → `no-store`;
    - everything else → `EVILFLOWERS_CACHE_CONTROL_DEFAULT` (`private, no-cache`) plus `Vary: Authorization`.

    `no-cache` means "store, but revalidate before every reuse". The existing `ETag` turns that revalidation into
    a cheap `304`, so freshness costs one round trip, not a full download. Because the middleware sits below
    `ConditionalGetMiddleware`, the `304` Django builds copies the same headers.

    It does not try to detect whether a request carries credentials. Tokens also travel in URLs and cookies, so
    the default is `private` for everyone. Views that know better keep their own header: OPDS feeds (#72), Readium
    content (`private, no-store`), notification branding, MCP metadata and the files endpoints.

2. **Covers and thumbnails** get a validator and revalidate on every use: `public, no-cache` plus an `ETag` built
   from the entry id, the file name and `touched_at`, and `Last-Modified` from `touched_at` (added with #126). A
   matching `If-None-Match`/`If-Modified-Since` is answered with a `304` before storage is touched, not even an
   `exists()` check. A new upload is a new file name, so the next request after a cover change gets the new
   image. `EVILFLOWERS_FILES_CACHE_CONTROL_PUBLIC` still overrides the policy.

### Edge nginx (knowledge repository, `ansible/roles/nginx`)

3. **Prod `/data/v1/`: drop the proxy cache.** Until Phase 1 ships it shares per-user answers. After that, every
   response under it is `private`, `no-cache` or `no-store`, which nginx never stores, so the cache would only
   hold leftovers and stay a trap for the next header-less response. Remove `proxy_cache*` from the location, the `evilflowers` zone from
   `conf.d/cache.conf` and the cache directory (`/mnt/data/var/cache/nginx/evilflowers`, including its
   four-week-old entries).
4. **SPA vhosts** (every vhost with `try_files … /index.html`: `elvira.stuba.sk`, `dev.elvira.stuba.sk`,
   `elvira.{fad,fei,fiit,mtf,sjf,svf}.stuba.sk`, `um.elvira.stuba.sk` if it is the same shape):
    - The policy is chosen by a `map $uri $spa_cache_control` in `conf.d/` and applied with **one server-level**
      `add_header Cache-Control $spa_cache_control always;`. It must not go in a location:
      `templates/vhost_http3.j2` adds `add_header Alt-Svc … always` at server level, and any `add_header` in a
      location drops every inherited one, so HTTP/3 would be silently lost.
    - Hashed chunks only (`^/assets/[^/]+-[A-Za-z0-9_-]{8}\.(js|css|…)$`) get
      `public, max-age=31536000, immutable`. Everything else, including `index.html` reached through
      `try_files` and the unhashed `public/` files (faculty logos, `thumbnail.webp`), gets `no-cache`; nginx
      answers revalidations with a `304` from `ETag`/`Last-Modified`. A plain `/assets/` prefix would be wrong:
      the portal's `public/assets/<faculty>/…` files share the folder and are not hashed.
    - A separate location with `try_files $uri =404` for the hashed pattern, without its own `add_header`.
5. **Observability:** where any `proxy_cache` remains, add `add_header X-Cache-Status $upstream_cache_status
   always;` so a cached answer can be seen in the browser.

## Implementation Plan

### Phase 1: Catalog ✅ on the working tree

- [x] `apps/api/middleware/cache_control.py` and its `MIDDLEWARE` entry below `ConditionalGetMiddleware`
- [x] `EVILFLOWERS_CACHE_CONTROL_DEFAULT` (`private, no-cache`); `EVILFLOWERS_FILES_CACHE_CONTROL_PUBLIC` default
  changed to `public, no-cache`
- [x] Covers and thumbnails: pre-render `ETag` + `304` (`apps/files/views.py` `_cover_response`)
- [x] Tests: `apps/core/tests/test_cache_control.py` (default, writes, errors, view override, 304 headers, cover
  revalidation without opening storage, replaced cover, private per-user `303` and base64 download), and `apps/readium/tests/test_license_return_freshness.py`
  (return, then list, for ACTIVE→returned and READY→cancelled)

### Phase 2: Edge nginx (knowledge repository, EvilFlowersCatalog/knowledge#27)

- [ ] Prod `evilflowers.elvira.stuba.sk`: remove `proxy_cache*` from `location ~ ^/data/v1/`
- [ ] `conf.d/cache.conf`: remove the `evilflowers` zone; remove its directory from `nginx_cache_dirs`; delete
  `/mnt/data/var/cache/nginx/evilflowers` on the gateway
- [ ] SPA vhosts: `map $uri $spa_cache_control` in `conf.d/`, one server-level `add_header`, a hashed-chunk
  location with `try_files $uri =404`
- [ ] **Deploy the cache removal first, without waiting for a catalog release.** It needs nothing from Phase 1,
  and it ends the shared 4-week caching of per-user `303`s and base64 downloads on prod.
- [ ] Verify with `curl -I`: the licenses list has `private, no-cache` and `Vary: Authorization`; covers have
  `public, no-cache` and `ETag`; `index.html` has `no-cache`; a hashed chunk is `immutable`; an unhashed
  `/assets/fiit/…` file is `no-cache`

### Phase 3: #123 in the portal (elvira-portal)

- [ ] Cancel in-flight license and reservation queries before the return `PUT`
  (`queryClient.cancelQueries`), or drop responses for requests that started before the mutation, and use the
  `PUT` response to update the card immediately
- [ ] Speed up `GET /readium/v1/licenses` (≈3 s for 50 loans). The nested entry serializer and
  `lcp_state_mapping` are the first suspects.

## Technical Details

| File | Change |
|---|---|
| `apps/api/middleware/cache_control.py` | new |
| `evil_flowers_catalog/settings/base.py` | middleware entry; `EVILFLOWERS_CACHE_CONTROL_DEFAULT`; files public default |
| `apps/files/views.py` | `_cover_etag`, `_cover_response` for covers and thumbnails |
| `apps/core/tests/test_cache_control.py` | new |
| `apps/readium/tests/test_license_return_freshness.py` | new |

How nginx treats upstream headers: it does not store a response with `private`, `no-cache`, `no-store` or
`max-age=0`, and an explicit upstream `Cache-Control` overrides `proxy_cache_valid`. That is why the proxy
cache becomes inert once Phase 1 ships, and why removing it loses nothing.

Proposed SPA shape. `conf.d/spa-cache.conf` (http context, deployed by the existing `conf.d/` copy task):

```nginx
# IP-027. Vite output only: flat in /assets/, name-<8 char hash>.ext. Unhashed public/ files live in subfolders.
map $uri $spa_cache_control {
    "~^/assets/[^/]+-[A-Za-z0-9_-]{8}\.(?:js|mjs|css|woff2?|ttf|png|jpe?g|gif|svg|webp|avif|wasm)$"
        "public, max-age=31536000, immutable";
    default "no-cache";
}
```

Each SPA vhost, root path unchanged:

```nginx
server {
    server_name <site>;
    # Server level, next to the Alt-Svc line vhost_http3.j2 inserts. No add_header in the locations below,
    # or they stop inheriting both.
    add_header Cache-Control $spa_cache_control always;

    location / {
        gzip_static on;
        root /mnt/data/var/www/<site>/current;
        try_files $uri $uri/ /index.html;
    }

    location ~ "^/assets/[^/]+-[A-Za-z0-9_-]{8}\.(?:js|mjs|css|woff2?|ttf|png|jpe?g|gif|svg|webp|avif|wasm)$" {
        gzip_static on;
        root /mnt/data/var/www/<site>/current;
        try_files $uri =404;
    }
    …
}
```

`$uri` is evaluated when the header is written, after `try_files` has rewritten a deep link to `/index.html`,
so the shell always gets `no-cache`. `try_files $uri =404` on hashed chunks makes a chunk deleted by a newer
deploy a `404`, not `index.html` served as JavaScript.

## Alternatives Considered

### Keep the nginx cache on `/data/v1/` with `proxy_cache_revalidate`

nginx cannot store a response that must be revalidated on every use (`no-cache`, `max-age=0`). Keeping the
cache would require covers with a real `max-age`, which brings back the staleness this proposal removes. See Q2.

### Version cover URLs (`?v=<touched_at>`) and serve them `immutable`

This gives the best caching, but changes every place that builds a cover URL (REST serializer, OPDS 1.2, OPDS 2
manifest) and the URL stored by OPDS clients. Revalidation gets the same freshness without that. It is worth
revisiting if cover traffic becomes a load problem.

### `no-store` everywhere

This is the simplest option, but it throws away the cheap `304` path that the OPDS feeds (#72) and the ETags
already give, and readers re-download unchanged lists in full.

## Trade-offs and Risks

- **One request per cover view.** Covers always reach Django, but a `304` is a single indexed query without
  storage I/O. Thumbnail grids of 50 cost 50 small requests per page view instead of 0 within the day. See Q2.
- **`private` also for anonymous readers.** A shared cache can no longer keep the public catalog JSON. Nothing
  shares it today (dev has no cache, prod's cache only covered `/data/v1/`).
- **Deploy order.** Phase 2 does not depend on Phase 1 and should go first: removing the cache only makes things
  fresher, and it ends a live cross-reader leak on prod.

## Success Criteria

- [ ] After a return on `/loans`, the card moves to "Previously borrowed" without a reload (needs Phase 3)
- [ ] Every API response on dev carries an explicit `Cache-Control`; authenticated JSON carries
  `Vary: Authorization`
- [ ] A changed cover is visible on the next page load
- [ ] After a portal deploy, an open browser picks up the new release on its next navigation without a hard
  reload

## Future Considerations

- Versioned cover URLs plus `immutable`, if cover traffic ever becomes a load problem (see Alternatives).
- A dedicated Vite `assetsDir`, so the edge rule for hashed chunks can be a plain prefix (Q3).
- Bump `touched_at` on author and category M2M edits. That would close the known gap the OPDS ETags (#72) and
  the cover ETags share.

## References

- EvilFlowersCatalog#123 (stale licenses list), #72 (OPDS conditional GET), #29 (files Cache-Control)
- RFC 9111 (HTTP Caching); nginx `ngx_http_proxy_module` (`proxy_cache_valid`, `proxy_ignore_headers`)
- Knowledge: `ansible/roles/nginx/files/sites/*`, `conf.d/cache.conf`; ADR-0004 (host nginx is the TLS edge)

## Discussion

None yet.

## Review Questions

**Status**: ⏳ Awaiting Answers
**Review Date**: 2026-11-09
**Reviewer**: Claude AI

The following questions must be answered before implementation is considered final. Phase 1 applies the
recommended option of each.

---

### Q1: #123 is a slow list plus a portal ordering race, not caching 🔴 Critical

**Issue**: The access log shows list `GET`s that started before the return `PUT` and answered after it. Two
refetches right after the `PUT` were aborted (`499`), and the first fresh list arrived about 5 s after the
return. Each list read takes ≈3.3 s (this repository). Phase 1 makes caching explicit, but cannot shorten that
window or stop the portal from showing an older response in the meantime.

**Question**: How should #123 be closed?

**Options**:
- [ ] **A**: Fix it in the portal (cancel in-flight queries before the mutation, apply the `PUT` response
  immediately) and speed up the list endpoint in a separate catalog issue; keep #123 open until the portal fix
  ships (recommended)
- [ ] **B**: Close #123 with Phase 1 and open a portal issue
- [ ] **C**: Add a server-side change too (for example, return the updated license list from the `PUT`)

**Answer**:
```
```

**Resolution**:
```
```

---

### Q2: Covers: always revalidate, or a short max-age? ⚠️ Medium

**Issue**: `public, no-cache` makes every cover view a request to Django (mostly a `304`) and keeps nginx from
caching covers at all. A short `max-age` (for example 300) would let browsers and nginx absorb thumbnail grids,
but a replaced cover would then appear up to 5 minutes late.

**Options**:
- [ ] **A**: `public, no-cache` + `ETag`, no nginx cache (implemented, recommended: freshest; the `304` is cheap)
- [ ] **B**: `public, max-age=300` + `ETag`, keep the nginx cache on `/data/v1/` with `proxy_cache_revalidate`
- [ ] **C**: Versioned cover URLs + `immutable` (see Alternatives)

**Answer**:
```
```

**Resolution**:
```
```

---

### Q3: Hashed-chunk regex and stale tabs after a deploy ⚠️ Medium

**Issue**: The `immutable` location relies on Vite's `name-<8 char hash>` naming and on unhashed `public/` files
living in subfolders of `/assets/`. A future flat, unhashed file such as `/assets/logo-darkmode.svg`
(8 characters after the dash) would wrongly be cached for a year. Separately, deploys replace `current/`, so a
tab still running the previous release gets `404` for lazy chunks that no longer exist.

**Options**:
- [ ] **A**: Use the regex as proposed; set Vite `build.assetsDir` to a dedicated folder (for example `_app/`)
  in a later portal change, so the rule can be a plain prefix (recommended)
- [ ] **B**: Change `assetsDir` now, together with Phase 2
- [ ] **C**: Also keep the previous release's chunks for one deploy (copy forward), so open tabs keep working

**Answer**:
```
```

**Resolution**:
```
```

---

### Q4: `kniznica.fiit.stuba.sk` caches unhashed CSS/JS for a year ℹ️ Low

**Issue**: That static site (`/var/www/html/sik`) sends `expires 1y` and `public, immutable` for every
`.css`/`.js`/image, but its file names carry no hash. An edited stylesheet is not picked up for up to a year.
It is outside the catalog but on the same edge.

**Options**:
- [ ] **A**: Switch it to `no-cache` for CSS/JS in Phase 2 (recommended)
- [ ] **B**: Leave it; the site rarely changes

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
| 2026-11-09 | jdubec | Initial draft for #123; Phase 1 implemented and tested locally (Claude) |
| 2026-11-09 | jdubec | Added Review Questions section |
| 2026-11-09 | jdubec | Phase 2 filed as knowledge#27 |
| 2026-11-09 | jdubec | Review fixes: #123 timeline incl. the fresh read at 22:12:55; prod per-user `303` leak and deploy order (nginx first); SPA headers via a server-level `map` so `Alt-Svc` survives; Future Considerations |
| 2026-11-09 | jdubec | Covers: `Last-Modified` and the `304` before `storage.exists()`, contributed by the #126 work in the same tree |
