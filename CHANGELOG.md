# Changelog

This changelog suppose to follow rules defined in the [changelog.md](https://changelog.md)

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
