# Changelog

This changelog suppose to follow rules defined in the [changelog.md](https://changelog.md)

## 0.16.0 : TBD

## 0.15.0 : 2026-11-12 (The Library Reads Back)

The catalog has learned to read its own books. It searches inside them, explains them and cites the page.
Then a k6 run made it sweat, so it now does all of that with a fraction of the queries. Read the
upgrade notes at the end before you deploy. The ⚠️ items in them are not optional.

### Discovery

- **Added**: Search rebuilt inside PostgreSQL:
  - full-text search with Slovak/Czech stemming, highlights and "did you mean" ([#101])
  - hybrid BM25 + semantic ranking (`multilingual-e5-large-instruct` served by `tei-embed`, IP-021, [#102])
  - search inside publications on `GET /api/v1/entries/passages` ([#60], [#113])
  - type-ahead on `GET /api/v1/entries/suggest`
- **Added**: Facets with exact counts on `GET /api/v1/entries/facets`, in the list (`include=facets`) and in OPDS 1.2 / 2.0 search feeds ([#74])
- **Added**: Ratings and reviews with moderation (IP-023, [#57])
- **Added**: Similar publications and entry figures from the paragraph index ([#62]). The PDF outline becomes the entry's table of contents
- **Added**: Reading history on `GET /api/v1/activity` (IP-016). Category and feed titles in English and Slovak (IP-017, [#88])
- **Changed**: Feed titles may repeat within a catalog; only `url_name` must be unique, and a taken one answers 409 with a suggestion ([elvira-portal#30])
- **Removed**: evilflowers-search-service integration: `SEARCH_SERVICE_URL`, `reindex_acquisitions`, and the `process_pdf` publish on upload ⚠️

### Assistant

- **Added**: A chat assistant on `/assistant/v1/`, built on the MCP tools (IP-015, [#77]). In the PDF viewer it explains a selection or a figure, cites pages and highlights passages ([#87], [#89])
- **Added**: Grounded answers (IP-024, [#115]): evidence labels, citation checks, versioned prompts, chapter summaries. It reads the books a reader mentions ([#127]) and runs on a self-hosted Ollama model (`EVILFLOWERS_ASSISTANT_OLLAMA_*`, [#104], [#130], [#138])
- **Added**: Extraction store (IP-018, [#92]): regions, paragraphs and pgvector embeddings, ingested on `/extraction/v1/`, plus `backfill_extraction`. Off by default (`EVILFLOWERS_EXTRACTION_ENABLED`). Readium LCP titles are never read by AI

### Lending and operations

- **Added**: Lending statistics for the admin dashboard on `GET /api/v1/statistics` (IP-028, [#131])
- **Added**: Encryption jobs record their Celery task and accept worker failure reports ([#99]). MCP `reencrypt_publication` re-encrypts registered jobs ([#98]). The LCP manifest carries full metadata ([#96])
- **Added**: MCP tools for publishing (`create_entry`, `create_upload_link`), users and API keys
- **Added**: Branded notification e-mails and e-mail import from LDAP (IP-025)
- **Added**: Users change their own password on `POST /api/v1/users/me/password` ([#8])
- **Added**: Request rate limits (IP-026, [#21]). Off by default: campus NAT puts many readers behind one IP
- **Fixed**: 1-week loans can be renewed ([#110]). Returning a never-opened loan sends its e-mail ([#109]). The licence list is fresh right after a return ([#123]). 0-byte uploads are refused ([#112])

### Performance (or: what k6 taught us)

- **Fixed**: N+1 queries are gone: list endpoints cost the same number of queries for any page size (`GET /api/v1/entries?limit=30` used to run 226 SELECTs) ([#124])
- **Fixed**: The RSA JWK is imported once per process. It used to be imported on every request, which cost a third of the CPU ([#125])
- **Changed**: Covers and thumbnails use versioned, immutable URLs. They answer 304 without touching storage and no longer care about tokens (IP-027, [#126], [#128])
- **Changed**: HTTP Range on downloads ([#70]). OPDS 1.2 conditional GET and feed paging ([#72])
- **Changed**: The entry list loads only what it serializes. Facets rank once ([#135]). Search waits at most 0.3 s for `tei-embed` ([#134])
- **Added**: Optional psycopg pool (`EVILFLOWERS_DB_POOL`, [#133]) and support for PgBouncer transaction pooling ([#103])
- **Added**: Celery settings for autoscaling: `CELERY_HOSTNAME`, `CELERY_WORKER_CONCURRENCY`, prefetch and late acks ([#106], [#107], [#129], [#142]). Broker visibility timeout of 12 h ([#120])

### Observability

- **Added**: OpenTelemetry traces, metrics and JSON logs (IP-019), semantic spans across the ecosystem (IP-029, [#139]) and Pyroscope profiling that stays cheap when idle ([#111], [#136])

### Housekeeping

- **Added**: Periodic cleanup of orphaned storage directories (`EVILFLOWERS_ORPHAN_CLEANUP_SCHEDULE`, off when unset, [#30])
- **Fixed**: `manage.py backup` works against PostgreSQL 18, fails loudly instead of "succeeding" and no longer spends 17 minutes xz-compressing PDFs ([#93], [#103])
- **Fixed**: The Celery event listener survives extraction tasks ([#108], [#122])
- **Fixed**: OPDS compatibility with Thorium (lending, borrow links, authentication document) and the LCP licence page in encrypted PDFs ([#86])

### Upgrade notes (0.15.0)

- ⚠️ PostgreSQL needs the `vector` and `pg_trgm` extensions; `pg_search` is optional and enables BM25. Migrations: core 0045–0050, extraction 0001–0005, readium 0008–0010, assistant 0005–0009, notifications 0004
- ⚠️ CORS is no longer open to every origin. Set `CORS_ALLOWED_ORIGINS`, or rely on the default, the origin of `EVILFLOWERS_PORTAL_URL` ([#100])
- ⚠️ MCP accepts only Bearer by default and checks the origin. Set `EVILFLOWERS_MCP_AUTHENTICATION_SCHEMAS=Bearer,Basic` to keep password clients working ([#100])
- New settings worth setting: `EVILFLOWERS_EMBEDDING_URL` (without it, search uses words only), `EVILFLOWERS_READIUM_LCPENCRYPT_REPORT_URL`, `EVILFLOWERS_BACKUP_PGHOST`
- After migrating: `manage.py embed_entries --missing`; `manage.py reindex_search` if the Slovak/Czech dictionaries were added later

[#8]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/8
[#21]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/21
[#30]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/30
[#88]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/88
[elvira-portal#30]: https://github.com/EvilFlowersCatalog/elvira-portal/issues/30
[#57]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/57
[#60]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/60
[#62]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/62
[#70]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/70
[#72]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/72
[#74]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/74
[#77]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/77
[#86]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/86
[#87]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/87
[#89]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/89
[#92]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/92
[#93]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pull/93
[#96]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/96
[#98]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/98
[#99]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/99
[#100]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/100
[#101]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/101
[#102]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/102
[#103]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/103
[#104]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/104
[#106]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/106
[#107]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/107
[#108]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/108
[#109]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/109
[#110]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/110
[#111]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/111
[#112]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/112
[#113]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/113
[#115]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/115
[#120]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/120
[#122]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/122
[#123]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/123
[#124]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/124
[#125]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/125
[#126]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/126
[#127]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/127
[#128]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/128
[#129]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/129
[#130]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/130
[#131]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/131
[#133]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/133
[#134]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/134
[#135]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/135
[#136]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/136
[#138]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/138
[#139]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/139
[#142]: https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/142

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
