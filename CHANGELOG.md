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
- **Added**: The lcpencrypt worker receives full publication metadata for the LCP-for-PDF package manifest — all authors in order, publisher, description, language, day-precision `published`, subjects, ISBN/DOI `identifier` and the catalog cover ([#96](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/96))
- **Added**: Kubernetes deployment in the build pipeline
- **Added**: Readium encryption jobs record the Celery `task_id`, `trace_id`, attempt count and last dispatch time; `POST /readium/v1/hooks/encryption/result` accepts the lcpencrypt worker's report of every task, so a worker failure turns the job `failed` with its error; both encryption webhooks tag their span with `lcp.content_id`; `list_encryption_jobs` returns the tracking fields, the last report and a `stale` flag, `requeue_encryption` the new `task_id`. Needs `EVILFLOWERS_READIUM_LCPENCRYPT_REPORT_URL` and migration `readium.0008` ⚠️ ([#99](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/99))
- **Added**: MCP tool `reencrypt_publication` (administrators) re-encrypts already `registered` publications, e.g. after a worker upgrade: by `content_ids` or `entry_ids`, keeps the LCP content id, skips jobs with `ready`/`active` loans unless `force` (reporting how many would break), `dry_run` shows the metadata that would be sent; returns each new dispatch's `task_id`/`trace_id` ([#98](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/98))
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
