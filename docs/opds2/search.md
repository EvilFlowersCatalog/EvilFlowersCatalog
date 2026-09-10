# OPDS 2.0 Search Modes

> IP-008 Phase 6 — Q5 resolution.

The OPDS 2.0 search endpoint accepts an optional `mode` parameter that
selects how the query is executed.

```
GET /opds/v2/{catalog}/search?query=<query>&mode=<mode>
```

The `query` parameter is required for `keyword` and `semantic`; for
`catalog` mode it is optional (an empty query returns the full
catalog-DB filter result, useful for browsing-via-search clients).

## Modes

### `mode=catalog` (default)

DB-side `EntryFilter` over `title`, `authors__name`, `categories__term`,
and the rest of the entry filter surface.

- No network call to the search service.
- Catalog-scoped via the standard ACL filter.
- Best for: navigation, browsing, fast results.

**Example**

```http
GET /opds/v2/stu/search?query=biology
```

### `mode=keyword`

Calls evilflowers-search-service `POST /search/elasticsearch` with the
catalog's accessible-acquisition allow-list.

- Matches inside the indexed PDF text content + metadata.
- Best for: looking inside the document.

**Example**

```http
GET /opds/v2/stu/search?query=mitochondria&mode=keyword
```

### `mode=semantic`

Calls evilflowers-search-service `POST /search/semantic` with the same
allow-list.

- Vector / embedding search.
- Best for: natural-language queries, "books about X".

**Example**

```http
GET /opds/v2/stu/search?query=research+on+cell+structure&mode=semantic
```

## Failure semantics

When `mode=keyword` or `mode=semantic`:

- **Search service unreachable / timeout** — `502 Bad Gateway` with
  `Retry-After: 10`. Clients should either retry or downgrade to
  `mode=catalog`.
- **Search service returns 5xx** — same as unreachable.
- **Search service returns a malformed body** — `502 Bad Gateway`
  without `Retry-After` (retrying a broken schema won't help).

`mode=catalog` never reaches the network and therefore never returns
502 from the search-service path.

## Catalog scoping

The search service has no concept of catalog membership. The OPDS 2.0
view computes the catalog's allowed acquisition UUIDs and passes them
as the `document_ids` field in the request to the search service. The
view also post-filters the results to acquisitions actually belonging
to the requested catalog as a defensive second check.

Cross-tenant leakage is therefore prevented in two places:

1. The allow-list sent to the search service.
2. The Django ORM filter on the result set.

A test in `apps/opds2/tests/test_search_modes.py` asserts that a
`mode=keyword` request matched on document IDs from another catalog
returns no entries.

## Anonymous users

Anonymous requests are restricted to public catalogs (`is_public=True`).
The allow-list passed to the search service follows the same rule.

## Configuration

```sh
SEARCH_SERVICE_URL=http://search-service:8000
SEARCH_SERVICE_TIMEOUT_SECONDS=10
```

If `SEARCH_SERVICE_URL` is unset, the OPDS 2.0 search view raises
`SearchServiceUnavailable` for any non-catalog mode — operators must
configure the URL before enabling keyword / semantic search.

## Operator commands

```sh
# Re-publish the text-service indexing jobs for an entire catalog.
python manage.py reindex_acquisitions --catalog stu

# Re-publish only acquisitions touched on or after a given date.
python manage.py reindex_acquisitions --since 2026-05-01

# Preview without enqueuing.
python manage.py reindex_acquisitions --catalog stu --dry-run
```

The text-service publish uses `task_id=text:index:{entry_id}` so
duplicate re-enqueues are coalesced by Celery — running this twice is
safe.
