# Changelog

This changelog suppose to follow rules defined in the [changelog.md](https://changelog.md)

## 0.9.0 : TBD

- **Added**: Application cache layer
- **Added**: `/data/v1/thumbnails/:entry_id` introduced
- **Changed**: Sort entries in Complete OPDS catalog by `created_at`
- **Changed**: `EntrySerializer` now returns URL path in the `thumbnail` property instead of base64 image (performance)
- **Fixed**: `page` -> `page_number` in AnnotationItemFilter
- **Fixed**: `exact` lookup fields on ForeignKeys

## 0.8.0 : 2024-05-09

The cache layer implemented in this release have been removed and will be present in `0.9.0` due to the performance
testing. I know it's kinda does not make any sense, but I want to have a relatable before sample.

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
