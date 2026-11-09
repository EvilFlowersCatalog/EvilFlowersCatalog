# Changelog

This changelog suppose to follow rules defined in the [changelog.md](https://changelog.md)

## 0.15.0 : Unreleased

- **Added**: In-catalog chat assistant on `/assistant/v1/` reusing the MCP tools (IP-015, [#77](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/77)); chats can be resumed, carry an attached publication (`entry_id` on messages) and answer in the reader's UI language
- **Added**: Assistant in the PDF viewer — `POST /assistant/v1/explain` explains a selection or a region, chats bind to the reader's copy and read it (`read_pdf_page`, `search_pdf` with `regex: true`), cite pages as `[[page:N]]` and store what the reader attached ([#87](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/87), [#89](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/89))
- **Added**: Assistant reads open-access PDFs of non-Readium entries; `EVILFLOWERS_ASSISTANT_MAX_PAGES_READ` (default 20) caps pages read per turn; Readium LCP entries are never read
- **Added**: Agent highlights — `POST/GET /assistant/v1/highlights` finds passages by semantic search over the paragraph index (agentic search as fallback) and stores them as an agent annotation; reader highlights carry `text` and `quads` (IP-018)
- **Added**: `apps/extraction` (IP-018, [#92](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/92)) — extraction runs, regions (figures, tables, equations), paragraphs with pgvector embeddings and sentences; ingestion on `/extraction/v1/`, reader regions on `/api/v1/user-acquisitions/{id}/regions`, `backfill_extraction` command; off by default (`EVILFLOWERS_EXTRACTION_ENABLED`), requires the `vector` extension in PostgreSQL ⚠️
- **Added**: Recommendations from the paragraph index — `GET /api/v1/entries/{id}/similar` and `POST /api/v1/user-acquisitions/{id}/similar` ([#62](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/62)); entry figures on `GET /api/v1/entries/{id}/figures` (not for Readium LCP entries)
- **Added**: PDF outline stored as the entry's table of contents after upload; `backfill_table_of_contents` management command
- **Added**: `GET /api/v1/entries/facets` with per-facet counts (languages, categories, feeds, availability) and publication year range; `reserved` pseudo-state in the `lcp_state` filter ([#74](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/74))
- **Added**: User activity history (IP-016) on `GET /api/v1/activity`; admin search over users, user acquisitions, activities and Readium loans
- **Added**: Localized titles of categories and feeds (IP-017, English and Slovak) ([#88](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/88))
- **Added**: MCP publishing tools (`create_entry`, `update_entry`, `create_upload_link` with signed single-use upload URLs), Readium encryption tools (`list_encryption_jobs`, `requeue_encryption`), user management (`list_users`, `get_user`, `create_user`, `update_user`, `delete_user`) and API key tools (`list_api_keys`, `create_api_key`, `revoke_api_key`)
- **Added**: OPDS 1.2 conditional GET with a user-aware `ETag` and RFC 5005 paging of acquisition feeds; `EVILFLOWERS_OPDS_PAGE_SIZE` (default 50) and `EVILFLOWERS_OPDS_COMPLETE_FEED_MAX_ENTRIES` (default 2000) ([#72](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/72))
- **Added**: OpenTelemetry observability — traces, metrics and JSON logs for Django, Celery, gunicorn, the assistant and MCP (IP-019, see `docs/observability.md`)
- **Added**: Continuous CPU profiling with the Pyroscope SDK (`pyroscope-io`, `pyroscope-otel` in the `otel` group): every gunicorn worker, Celery child, beat and the event handler starts the profiler after it has forked when `PYROSCOPE_SERVER_ADDRESS` is set (unset = off), so profiles name Python views, ORM calls and tasks where the eBPF profiler cannot unwind Python. `PYROSCOPE_APPLICATION_NAME`, `PYROSCOPE_TAGS` (plus a `role` tag per process). Root spans carry `pyroscope.profile.id` for Tempo's trace-to-profile link, except in the gevent gunicorn workers ([#111](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/111))
- **Added**: The lcpencrypt worker receives full publication metadata for the LCP-for-PDF package manifest — all authors in order, publisher, description, language, day-precision `published`, subjects, ISBN/DOI `identifier` and the catalog cover ([#96](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/96))
- **Added**: Kubernetes deployment in the build pipeline
- **Added**: Readium encryption jobs record the Celery `task_id`, `trace_id`, attempt count and last dispatch time; `POST /readium/v1/hooks/encryption/result` accepts the lcpencrypt worker's report of every task, so a worker failure turns the job `failed` with its error; both encryption webhooks tag their span with `lcp.content_id`; `list_encryption_jobs` returns the tracking fields, the last report and a `stale` flag, `requeue_encryption` the new `task_id`. Needs `EVILFLOWERS_READIUM_LCPENCRYPT_REPORT_URL` and migration `readium.0008` ⚠️ ([#99](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/99))
- **Added**: MCP tool `reencrypt_publication` (administrators) re-encrypts already `registered` publications, e.g. after a worker upgrade: by `content_ids` or `entry_ids`, keeps the LCP content id, skips jobs with `ready`/`active` loans unless `force` (reporting how many would break), `dry_run` shows the metadata that would be sent; returns each new dispatch's `task_id`/`trace_id` ([#98](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/98))
- **Added**: In-database full-text search (IP-020, [#101](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/101)) — `entries.search_vector` (title A, authors + category labels B, publisher + summary C), stemmed for the entry's language and indexed accent-free; `query` understands web-search syntax (`"phrase"`, `-word`, `or`), matches inflected Slovak through a prefix fallback, falls back to any word and then to similar titles/authors when nothing matches, and is ranked by relevance by default (`order_by=relevance` explicitly). Items of `GET /api/v1/entries?query=` carry `search` (score, matched fields, `<mark>` highlights), `metadata.search` reports `relaxed`/`fallback`/`did_you_mean`, `include=facets` returns the facets in the same response. Migration `core.0046` needs `pg_trgm` and creates the `evilflowers_*` text search configurations; Slovak/Czech hunspell configurations are created when the PostgreSQL image ships the dictionaries ([evilflowers-postgresql#1](https://github.com/EvilFlowersCatalog/evilflowers-postgresql/issues/1)) — then run `manage.py reindex_search` and restart the application ⚠️
- **Added**: Hybrid search (IP-021, [#102](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/102)) — the words are ranked by BM25 (ParadeDB `pg_search`, created by migration `core.0047` when the extension is available; English/German/Czech-stemmed and accent-folded fields) and fused by weighted Reciprocal Rank Fusion with the entries closest in meaning (`entries.embedding`, `halfvec(1024)` from `multilingual-e5-large-instruct` served by the shared `tei-embed`). `metadata.search.mode` is `hybrid`, `bm25`, `fulltext` or `semantic`, `fallback: "semantic"` marks results found by meaning only, items report `matched: ["semantic"]`; without TEI (or when it is down, `degraded: true`) search is words only. Entries are embedded by a Celery task after every change; `manage.py embed_entries --missing` backfills. Settings `EVILFLOWERS_EMBEDDING_URL`, `EVILFLOWERS_EMBEDDING_API_KEY`, `EVILFLOWERS_SEARCH_VECTOR_MIN_SCORE` (0.8), `EVILFLOWERS_SEARCH_VECTOR_WEIGHT` (0.5) ⚠️
- **Added**: Search inside publications — `GET /api/v1/entries/passages?query=` ranks the paragraphs of every processed PDF the caller may see (other `/api/v1/entries` filters narrow it) and returns the best passages per publication with page, section and score; the text is withheld for Readium LCP entries and outside the entry's allowed IP ranges; `degraded` without TEI, `indexed` counts searchable publications. `EVILFLOWERS_SEARCH_PASSAGE_MIN_SCORE` (0.8) ([#60](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/60))
- **Added**: Ratings and reviews (IP-023, [#57](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/57)) — readers rate an entry 1–5 with an optional title and text, one review per user: `PUT/GET/DELETE /api/v1/entries/{id}/reviews/mine`, `GET /api/v1/entries/{id}/reviews` (published reviews, `order_by` `-created_at`/`-rating`); catalog managers hide or publish a review with `PATCH /api/v1/reviews/{id}` or in the admin, and hidden reviews do not count. Entries carry `rating: {average, count, mine}`; `GET /api/v1/entries` accepts `order_by=-rating` (Bayesian average, `EVILFLOWERS_REVIEWS_PRIOR_MEAN` 3.0 / `EVILFLOWERS_REVIEWS_PRIOR_WEIGHT` 5) and `rating_min`; MCP entries carry `rating`. `EVILFLOWERS_REVIEWS_ENABLED` (default on), `EVILFLOWERS_REVIEWS_BODY_MAX_CHARS` (5000), `manage.py recompute_ratings [--check]`. Migration `core.0048` ⚠️
- **Added**: HTTP Range on acquisition, cover, thumbnail and Readium LCP content downloads — `Accept-Ranges: bytes`, `206 Partial Content` with `Content-Range` for one byte range, `416` past the end, an accurate `Content-Length`; S3 reads only the requested bytes. Popularity and download activity count the first request of a ranged download only. Personalised PDFs (`EVILFLOWERS_MODIFIERS`, the user-acquisition download) are still served whole: each render differs, so ranges cannot be combined. `GZipMiddleware` no longer compresses file responses (it dropped `Content-Length`) ([#70](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/70))
- **Added**: Periodic orphan cleanup — `apps.tasks.tasks.cleanup_orphans` deletes storage directories no catalog or entry owns, keeping any with a file newer than `EVILFLOWERS_ORPHAN_CLEANUP_MIN_AGE_HOURS` (default 168); scheduled by `EVILFLOWERS_ORPHAN_CLEANUP_SCHEDULE` (cron, unset = off). `storage_audit --min-age-hours` applies the same guard by hand ([#30](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/30))
- **Added**: PgBouncer transaction pooling ([#103](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/103)) — `PGBOUNCER_TRANSACTION_POOLING=1` sets `DISABLE_SERVER_SIDE_CURSORS`; `EVILFLOWERS_BACKUP_PGHOST` / `EVILFLOWERS_BACKUP_PGPORT` point `pg_dump` at the server instead of the pooler. No session-level state found (no `SET` GUCs, advisory locks, `LISTEN` or SQL `PREPARE`)
- **Fixed**: `manage.py backup` raises when `pg_dump`, the upload or a catalog dump fails, so the `backup` Celery task fails instead of reporting success ([#103](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/103))
- **Fixed**: OPDS 1.2 feeds no longer answer `304` after an entry's authors or categories change, an author or category is renamed, or an acquisition is deleted — these bump the entry's `touched_at` ([#72](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/72))
- **Fixed**: The Celery event listener (`celery_event_handler`) no longer exits on an event it cannot store; it logs and skips it. Extraction tasks are sent under a deterministic UUID task id (`uuid5` of `extract:<acquisition>:v<N>`) instead of the `extract:…` string `JobProtocol` rejected ([#108](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/108))
- **Added**: `GET /api/v1/entries/suggest` — type-ahead entries, authors, categories and feeds with counts in one call
- **Added**: Facets `authors`, `publishers`, `formats` and `fulltext` with matching `publisher`, `format` and `fulltext` entry filters; `facet`, `facet_limit` and `facet_query` options; availability counts read only LCP entries' ids and config
- **Added**: OPDS facets — OPDS 2.0 search and publications feeds carry `facets` (with `numberOfItems`, active facet `rel: self`; the publications feed accepts the entry filters), OPDS 1.2 search feeds `rel="http://opds-spec.org/facet"` links with `opds:facetGroup`, `opds:activeFacet` and `thr:count`; the OpenSearch descriptor declares `language_code`, `category_id`, `author_id`, `feed_id` and the publication date range as optional `ef:` parameters; parameters sent back unfilled (`{…}`) are ignored
- **Changed**: One embedding model for entries, paragraphs and queries (IP-021): `extraction_paragraph.embedding` and `extraction_run.embedding` are `halfvec(1024)` (migration `extraction.0004` drops the 768-dimension Arctic vectors), queries are embedded by `tei-embed` instead of a Celery round-trip to text-service, `EVILFLOWERS_EXTRACTION_PIPELINE_VERSION` defaults to 2 — run `backfill_extraction --outdated` once text-service embeds through `tei-embed` ⚠️; `GET /api/v1/entries/{id}/similar` uses the entry vector, so it works without a processed PDF
- **Changed**: OPDS 2.0 `mode=keyword` (words only) and `mode=semantic` (meaning only) run in PostgreSQL; the OPDS 2.0 publications feed and search no longer answer 502
- **Removed**: evilflowers-search-service integration (IP-021) — `apps/api/services/search_service_client.py`, `SEARCH_SERVICE_URL` / `SEARCH_SERVICE_TIMEOUT_SECONDS`, the `process_pdf` send to text-service on upload (`apps/dataverse/services/text_publish.py`), `manage.py reindex_acquisitions` and the `search-service` compose service ⚠️
- **Fixed**: `-word` in a search excludes the word in every language configuration (a stemmer that split it differently let it back in)
- **Fixed**: Slovak/Czech text search configurations drop stop words before hunspell (`slovak_stop` / `czech_stop`, as the evilflowers-postgresql README prescribes); diacritics-free queries are highlighted in Slovak/Czech entries
- **Changed**: Hardened defaults ([#100](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/100)) ⚠️ — CORS comes from the environment: `CORS_ALLOWED_ORIGINS` (comma-separated, default the origin of `EVILFLOWERS_PORTAL_URL`), `CORS_ALLOW_ALL_ORIGINS=1` is now an explicit opt-in; the MCP DNS-rebinding guard is on by default (`EVILFLOWERS_MCP_ALLOWED_ORIGINS` defaults to the origin of `EVILFLOWERS_BASE_URL`, `*` disables it); `EVILFLOWERS_MCP_AUTHENTICATION_SCHEMAS` defaults to `Bearer` — set `Bearer,Basic` to keep username/password MCP clients working
- **Changed**: Docker image ships PostgreSQL 18 client tools from PGDG (`PG_MAJOR` build arg) ([#93](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/93), [#94](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/94))
- **Changed**: `compose.services.yml` runs the lcpencrypt worker from `:develop`, drops the unused `LCPSV_URL` and documents its OpenTelemetry settings ([#95](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/95))
- **Changed**: Feed titles no longer have to be unique within a catalog — only `url_name` is (migration `core.0045`); a taken `url_name` answers 409 `/feed-url-name-taken` naming the conflicting feed and a free `suggested_url_name`; `GET /api/v1/feeds` filters by exact `url_name` ([elvira-portal#30](https://github.com/EvilFlowersCatalog/elvira-portal/issues/30))
- **Changed**: Category `label` included in the categories Base serializer ([#76](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/76))
- **Fixed**: OPDS 1.2 / OPDS 2.0 compatibility with reading systems (Thorium lending, borrow links, authentication document, publication manifests)
- **Fixed**: Readium LCP license page in encrypted PDFs, encryption notifications and download headers ([#86](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/86))
- **Fixed**: Reservations are not promoted on entries with lending switched off
- **Fixed**: Status document link rewrite and anonymous entry listing
- **Fixed**: `GET /api/v1/entries?query=` keeps the relevance ranking instead of falling back to `created_at`; an explicit `order_by` still wins ([#60](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/60))
- **Fixed**: ISBN/DOI already stored as a URI (`urn:isbn:…`, `https://doi.org/…`) is no longer prefixed twice in OPDS identifiers and in the LCP package manifest; the manifest now uses the same identifier URI as the feeds
- **Fixed**: CI Docker build fails at "Install poetry" on a stale apt index ([#97](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/97))
- **Fixed**: N+1 queries in list endpoints ([#124](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/124)) — `GET /api/v1/entries`, `/api/v1/feeds`, `/api/v1/activity`, `/api/v1/shelf-records`, `/api/v1/user-acquisitions`, `/readium/v1/licenses` and `/readium/v1/reservations` now cost the same number of queries for any page size: the feed serializer reads prefetched `parents`/`children`, feeds come with their catalog, and every view nesting entries prefetches the same entry graph (`with_entry_graph`)
- **Fixed**: Bearer authentication imports the RSA JWK once per process instead of on every request (a third of the app's CPU on dev) and verifies with its public half; an API key's `last_seen_at` is written at most once a minute instead of saving the whole row per request ([#125](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/125))
- **Fixed**: Covers and thumbnails ([#126](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/126)) ignore any credential (they are not access-controlled; IP-010) and are not rate limited, load only the columns they serve, and answer `If-None-Match`/`If-Modified-Since` with a 304 before storage is touched; `Last-Modified` is sent. An entry without a cover answers 404 instead of 500
- **Fixed**: A search no longer waits ~9 s while `tei-embed` is slow: a reader's query is not retried on timeouts or connection errors (429/503 still are), and for 30 s after a failure searches skip TEI and stay lexical
- **Fixed**: The Celery event listener stores task arguments holding sets, tuples or bytes (`set is not JSON serializable` lost the job protocol)
- **Changed**: Extraction ingest validates a paragraph `embedding` as one list of finite numbers instead of a form field per dimension (~5 % of the app's CPU on dev); a number sent as a string is now rejected

## 0.14.0 : 2026-09-10

- **Added**: Model Context Protocol server at `POST /mcp/v1` (IP-014) — 25 tools over the catalog for LLM agents, plus resources, prompts and argument completions, authenticated with the existing credentials
- **Added**: MCP curation tools — catalog create/update/delete, bulk category import (`create_categories`), bulk classification (`classify_entries`), and incremental feed membership (`add_entries_to_feed`, `remove_entries_from_feed`)
- **Added**: MCP accepts HTTP Basic (username/password) alongside Bearer API keys; `EVILFLOWERS_MCP_AUTHENTICATION_SCHEMAS` defaults to `Bearer,Basic`
- **Added**: `EVILFLOWERS_MCP_MAX_BULK_ITEMS` (default 100) bounding one bulk MCP write
- **Added**: Proper Readium LCP integration
- **Added**: OPDS2 support
- **Added**: Dataverse integration for importing published Dataverse datasets as catalog entries and acquisitions
- **Added**: Dataverse pre-publish workflow hook, bootstrap script, and local Docker Compose stack
- **Fixed**: Dataverse publish workflow resume now runs asynchronously so the Dataverse GUI can finish publishing
- **Fixed**: Solr startup repairs an incomplete `collection1` core before Dataverse indexing

## 0.13.0 : 2026-03-01

- **Added**: Event broker system with support for Celery and Kafka
- **Changed**: Background tasks now use the event broker system
- **Changed**: Updated dependencies: argon2-cffi (^25.1.0), pymupdf (^1.26.3), black (^25.1.0)
- **Changed**: Using custom isbnlib from EvilFlowersCatalog GitHub repository
- **Changed**: Removed database connection pooling option (we use pgbounce now)
- **Changed**: rename `evilflowres_metadata_fetch` to `evilflowers_metadata_fetch`
- **Fixed**: PDF generation no longer uses linear parameter in tobytes()

## 0.12.2 : 2025-03-18

- **Fixed**: `python manage.py loadcatalog` S3 support

## 0.12.1 : 2025-03-13

- **Fixed**: You same PostgreSQL client binaries major version as on server (17.x)

## 0.12.0 : 2025-03-10

- **Added**: Server-side annotation render for user acquisitions using `?annotations=true` query param
- **Added**: Backup management command and Celery beat
- **Added**: Database connection pools enabled
- **Added**: `gevent` worker class is used with `gunicorn` in Docker image (`GUNICORN_WORKERS` introduced)
- **Added**: `entry_defaults` management command introduced and executed on server start
- **Changed**: Enhanced / extended S3 support all around
- **Changed**: `EVIL_FLOWERS_USER_ACQUISITION_MODE` -> `EVILFLOWERS_USER_ACQUISITION_MODE` ⚠️
- **Changed**: License files
- **Fixed**: Catch `FileNotFoundError` in `/v1/data` endpoints
- **Fixed**: Implicit Category creation in new Entry
- **Fixed**: No not rely on existence of all keys in ISBN introspection driver
- **Fixed**: Keep usernames from LDAP always in lower
- **Chores**: Migration from extras to groups in Poetry (`--with` instead of `-E`)

## 0.11.0 : 2024-10-10 (Too Drunk To Fuck)

This release is mainly focused on fixing bugs related to the production deployment on Slovak University of Technology.
The title of the release is dedicated to the song by punk legend Dead Kennedys.

As of this release - Docker images are based on Python 3.13.

- **Added**: `--skip-files` introduced in `dump_catalog` / `load_catalog` management commands
- **Added**: `unusablepassword` management command
- **Changed**: libpq compatible [environment variables](https://www.postgresql.org/docs/current/libpq-envars.html)
- **Changed**: Status endpoint now check if all supervisord processes are running
- **Fixed**: Fixed `TypeError` caused by `AnonymousUser` in `CatalogChecker`
- **Fixed**: Validate if `parent_id` in FeedFilter is valid `UUID`
- **Fixed**: Static URLs for shelf records fixed (`request` missing in serialization context)
- **Fixed**: Fixed many-to-many relationships in `dump_catalog` / `load_catalog` commands (`unique_together`)

## 0.10.0 : 2024-09-17 (STU Release)

- **Added**: Refactor of `/opds/v1.2/:catalog/search` (`SearchDescriptorView`)
- **Added**: `GET /api/v1/languages` introduced 🤷‍♂️
- **Added**: Celery shared task queue
- **Added**: Acquisition metadata update endpoint `PUT /api/v1/acquisitions/:acquisition` introduced
- **Added**: Acquisition pagination endpoint `GET /api/v1/acquisitions` introduced
- **Added**: Dump / load catalogs as tar files from CLI
- **Added**: Purge catalog from CLI
- **Changed**: Acquisitions in `GET /api/v1/entries` (`EntrySerializer.Base` now includes `Acquisitions.Base`) list
- **Changed**: `/opds/v1.2/:catalog/entries/:entry` updated to use `pydantic-xml`
- **Changed**: Current `User` is no longer context of the `PaginationResponse` serialization proces
- **Changed**: Shelf records of the User are now precalculated once before serialization and passed as context
- **Fixed**: OpenApi generator now shows also bodies and filter parameters
- **Fixed**: Language population in `EntryForm` using `AliasStrategy`
- **Fixed**: Correct processing of shelf records in Entry detail
- **Removed**: Support for CRON jobs (replaced by Celery)
- **Removed**: `BASE_URL` setting

## 0.9.0 : 2024-08-06 (The Amsterdam Release)

Finally, the complete RESTful documentation is available thanks to the `apps.openapi` module which generates
specification from the source code (I am lazy as fuck..). Som minor changes related to the traffic and computing
optimizations (getting rid of the base64 in REST endpoints).

- **Added**: Summary in `EntrySerializer.Base`
- **Added**: `/data/v1/thumbnails/:entry_id` introduced
- **Added**: OpenAPI generator
- **Changed**: Sort entries in Complete OPDS catalog by `created_at`
- **Changed**: `EntrySerializer` now returns URL path in the `thumbnail` property instead of base64 image (performance)
- **Changed**: `SECURED_VIEW_JWK` can be `None`
- **Changed**: `sentry-sdk` doesn't have to be installed (local import checks added)
- **Changed**: `EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR` is default  to `data/evilflowers/private`
- **Fixed**: `page` -> `page_number` in `AnnotationItemFilter`
- **Fixed**: `exact` lookup fields on ForeignKeys

## 0.8.0 : 2024-05-09 (The Norway Release)

The cache layer implemented in this release have been removed and will be present in future releases due to the
performance testing. I know it's kinda does not make any sense, but I want to have a relatable before sample.

🌈 Pink fluffy unicorns dancing on rainbow 🌈

- **Added**: Elastic APM support
- **Added**: Enabled Django Admin
- **Added**: `/opds/v1.2/:catalog/popular` introduced
- **Added**: `/opds/v1.2/:catalog/new` introduced
- **Added**: `/opds/v1.2/:catalog/shelf` introduced
- **Added**: Possibility to pass `Bearer` token using `access_token` query parameter in HTTP requests
- **Changed**: `author_id` and `contributors` merged into `EntryAuthor`
- **Changed**: APM client libraries are now optional using group `apm`
- **Changed**: Unified cache access (Redis backend)
- **Fixed**: `evilflowres_metadata_fetch` -> `evilflowers_metadata_fetch` in `EntryConfig`
- **Fixed**: `query` param in `EntryFilter` and `ShelfRecordFilter`

## 0.7.2 : 2024-05-07

- **Fixed**: Save user before assigning the catalogs

## 0.7.1 : 2024-02-11

- **Added**: `SECURE_SSL_REDIRECT` is now configurable with env variables
- **Added**: `SECURE_PROXY_SSL_HEADER` for production environment

## 0.7.0 : 2024-02-11

- **Added**: Basic OPDS 1.2 support
- **Added**: Categories CRUD endpoints in management API
- **Added**: `AuthorizationException` introduced (if anonymous user tries to access protected resource - return 401)
- **Added**: `EVILFLOWERS_ENFORCE_USER_ACQUISITIONS` introduced
- **Added**: [touched_at](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/1) introduced
- **Changed**: Migrated from porcupine-python to pydantic 2.0 for serializers
[Getting rid of the porcupine-python dependency](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/23)
- **Changed**: Read version from `pyproject.toml` instead of the `VERSION.txt`
- **Changed**: Complete `Annotation` refactor - support for pages introduced

## 0.6.0 : 2023-11-16

- **Added**: Introduced new Entry types: `publisher` and `published_at` (optional)
- **Changed**: Entries with same title, ISBN or DOI are not allowed (HTTP Conflict is thrown)
- **Changed**: Query entries also by feed name
- **Changed**: Catalog based uniqueness for feeds
- **Fixed**: Anonymous download of the user acquisition
- **Fixed**: Nullable citation and identifiers in Entry
- **Fixed**: ISBN fetch manipulation
- **Fixed**: Fixed processing of passwords containing some special characters

## 0.5.0 : 2023-09-09

- **Added**: Short-live access tokens: `GET /api/v1/token` and `GET /api/v1/token/refresh` introduced
- **Added**: LDAP support
- **Added**: Acquisition checksums
- **Added**: Extended validation errors
- **Added**: Multiple storage support (FileSystem and S3)
- **Added**: User acquisitions
- **Added**: Annotations
- **Added**: Added support for multiple storage backends (`FileSystemStorage` and `S3Storage`)
- **Added**: `EntryConfig` structure introduced
- **Added**: Citations (and ISBN autofetch - basic implementation)
- **Added**: Shelf records
- **Changed**: JWT tokens
- **Changed**: Debian based docker images instead of Alpine Linux
- **Changed**: Auth refactor
- **Changed**: [CORS](https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS) enabled for all origins
- **Changed**: Extended security model (ABAC introduced catalog access modes)
- **Changed**: Identifiers are now represented as a dictionary (`HStore` in database)
- **Changed**: Removed soft-delete

## 0.4.0 : 2022-02-03

- **Added**: Images and thumbnails
- **Added**: Entry and categories are m:n
- **Added**: Celery replaced by CRON jobs for periodic tasks
- **Added**: Status endpoint `GET /api/v1/status`
- **Changed**: Load countries and languages from [pycountry](https://github.com/flyingcircusio/pycountry)
- **Changed**: Django 4.0

## 0.3.0 : 2021-08-30

- **Added**: `Feed` can have multiple parents
- **Added**: `OpdsView` introduced
- **Added**: Public/Private `Catalog`
- **Added**: OPDS facets
- **Added**: Complete OPDS catalog acquisition feed
- **Changed**: URL routes for OPDS

## 0.2.0 - 2021-08-26

- **Added**: Extended CRUDs in RESTful API (authors, entries)
- **Added**: Containerisation
- **Added**: Acquisition uploads

## 0.1.0 - 2021-06-23

Initial minimum valuable product pre-release.

- **Changed**: Multiple catalogs database design
- **Added**: `Catalog`, `Feed` and `Entry` CRUD
- **Added**: Simple OPDS catalog
- **Added**: HTTP Authorization
