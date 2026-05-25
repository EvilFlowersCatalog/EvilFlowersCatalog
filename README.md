# Evil Flowers Catalog

<p align="center">
    <img width="150" height="150" src="docs/images/logo.png">
</p>

A publication catalog server compatible with [OPDS 1.2](https://specs.opds.io/opds-1.2) and
[OPDS 2.0](https://drafts.opds.io/opds-2.0), written in Python with a straightforward management REST API for CRUD
operations and end-to-end Readium LCP DRM support.

## Features

We are aware that the current documentation may not be satisfactory, and we are actively working to improve it. Right
now there is available at least complete documentation for the REST endpoints using OpenAPI
here: [https://elvira.digital/EvilFlowersCatalog/](https://elvira.digital/EvilFlowersCatalog/) If you have any
questions regarding usage, feel free to
[open an issue for clarification](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/new?assignees=&labels=documentation%2C+help+wanted%2C+question&projects=&template=request-for-clarification.md&title=),
[start a discussion](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/discussions),
or [contact us directly](mailto:jakub.dubec@stuba.sk).

The current list of features:

- **OPDS 1.2**: Access your publications using [OPDS 1.2](https://specs.opds.io/opds-1.2) — suitable for e-book
  readers.
- **OPDS 2.0**: Full [OPDS 2.0](https://drafts.opds.io/opds-2.0) JSON server with navigation, search, custom feeds,
  pagination, and Readium LCP borrowing — compatible with Thorium Reader and other modern LCP-capable reading apps.
- **Readium LCP**: End-to-end DRM lending pipeline (encrypt → license → borrow → reserve → renew → return) with
  reservation queue, oversharing detection, capability tokens, and a fully proxied LSD surface.
- **REST API**: Facilitates easy access and manipulation of catalog data for developers through CRUD operations.
- **Multiple storage options**: Store your documents on the filesystem or any S3-compatible object store.
- **Multi-tenant Catalog**: Supports multiple tenants, allowing separate catalogs for different user groups within the
  same server instance.
- **Authentication Support**: Local users, LDAP, JWT access/refresh tokens, and long-lived API keys.
- **Custom Feeds**: Users can organize publications into custom feeds, tailoring the catalog to specific needs or
  themes.
- **Publications Sharing**: Enables users to share publications with others, facilitating collaboration and
  distribution.
- **Annotation Storage**: Integrates with the EvilFlowersViewer project for storing annotations, enhancing the reading
  experience with personalized notes and highlights.
- **PDF Slicing & Editing**: Provides tools for slicing and editing PDF documents directly within the catalog, allowing
  for custom modifications and adjustments.
- **Dataverse integration**: Imports published Dataverse datasets into Evil Flowers Catalog through a Dataverse
  publish workflow, preserving dataset metadata and file links as catalog entries and acquisitions.
- **Search service**: Optional integration with an external keyword/semantic search backend (IP-008).
- **Notifications**: MJML-based email notifications for license, reservation, and passphrase events (IP-002).
- **Asynchronous Task Processing with Celery**: EvilFlowers Catalog leverages a Celery-based distributed task system to
  handle resource-intensive jobs — OCR processing, data extraction, LCP encryption, license-lifecycle sweeps, daily
  reminders, and database backups — through dedicated workers and beat schedules.

The implementation is based on these RFCs and specifications:

- [RFC7807: Problem Details for HTTP APIs](https://datatracker.ietf.org/doc/html/rfc7807)
- [RFC7617: The 'Basic' HTTP Authentication Scheme](https://datatracker.ietf.org/doc/html/rfc7617)
- [RFC6750: The OAuth 2.0 Authorization Framework: Bearer Token Usage](https://datatracker.ietf.org/doc/html/rfc6750)
- [RFC7519: JSON Web Token (JWT)](https://datatracker.ietf.org/doc/html/rfc7519)
- [OPDS 1.2](https://specs.opds.io/opds-1.2) and [OPDS 2.0](https://drafts.opds.io/opds-2.0)
- [Readium LCP](https://readium.org/lcp-specs/) and [Readium Web Publication Manifest](https://readium.org/webpub-manifest/)

## Project status

The project is in active production use. Major specifications — OPDS 1.2, OPDS 2.0 (navigation, search, pagination,
facets), and Readium LCP — are implemented end-to-end. New features are tracked as **Implementation Proposals (IP-XXX)**
under [`docs/proposals/posts/`](docs/proposals/posts); see the [proposals index](docs/proposals/index.md) for current
status. The REST API remains under active iteration — if you plan to deploy, please
[open a discussion](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/discussions) or
[email us](mailto:jakub.dubec@stuba.sk).

## Installation

### Docker

A pre-built Docker image is available on the GitHub Container registry as
[evilflowerscatalog](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pkgs/container/evilflowerscatalog).

The repository contains a working `compose.yml` configured for a development environment, with sidecars for
PostgreSQL, Redis, MinIO, Dataverse, the Readium LCP server, and the external workers (`evilflowers-ocr-worker`,
`evilflowers-lcpencrypt-worker`, `evilflowers-text-service`, `evil-flowers-search-service`). You can use a similar
configuration for production. The application image is built from the source.

Setup steps (service name `django` per `compose.yml`):

1. Initialize the stack: `docker compose up -d --build`
2. Seed languages and currencies: `docker compose exec django python3 manage.py setup`
3. Create a superuser: `docker compose exec django python3 manage.py createsuperuser`

The server will start on port 8000. For a Readium LCP-enabled deployment with shared filesystem storage between
Django, the LCP Server, and the lcpencrypt worker, follow the dedicated
[Readium LCP Deployment guide](docs/catalog-wiki/Readium-LCP-Deployment.md).

### Dataverse

The Docker setup includes a local [Dataverse](https://dataverse.org/) instance through `dataverse/compose.yml`.

In the local development stack:

- Evil Flowers Catalog runs at `http://localhost:8000`.
- Dataverse runs at `http://localhost:8080`.
- The default Dataverse admin login is username `dataverseAdmin` with password `admin1`.
- Solr runs at `http://localhost:8983` and is used by Dataverse for GUI/search indexing.

How the integration works:

1. A Dataverse pre-publish workflow is registered from `dataverse/hooks/prepublish-sync.json`.
2. When a user clicks **Publish** in the Dataverse GUI, Dataverse calls the Django endpoint
   `POST /api/v1/dataverse-prepublish`.
3. Django validates `DATAVERSE_WORKFLOW_SECRET`, fetches dataset metadata and files from Dataverse using
   `DATAVERSE_API_TOKEN`, and creates or updates an Evil Flowers `Entry` plus its Dataverse file acquisitions.
4. Django resumes the Dataverse workflow asynchronously so Dataverse can finish the publish operation and the GUI can
   show the dataset as released.

Local setup:

1. Start the stack:
   ```bash
   docker compose up -d --build
   ```
2. Run the normal Evil Flowers setup commands if this is a fresh database:
   ```bash
   docker compose exec django python3 manage.py setup
   docker compose exec django python3 manage.py createsuperuser
   ```
3. Open Dataverse at `http://localhost:8080` and log in as user: `dataverseAdmin`, password: `admin1`.
4. Create or copy a Dataverse API token for the admin user and set the same value as `DATAVERSE_API_TOKEN` in `compose.yml`.
   Then restart Django:
   ```bash
   docker compose up -d django
   ```
5. Register the Dataverse pre-publish workflow:
   ```bash
   export DATAVERSE_API_TOKEN="your-dataverse-api-token"
   dataverse/scripts/hook.sh "$DATAVERSE_API_TOKEN" --url http://127.0.0.1:8080 --trigger pre --file dataverse/hooks/prepublish-sync.json
   ```
6. Publish a dataset from the Dataverse GUI. During publish, Dataverse sends the dataset to Django; after Django
   imports it, Dataverse completes the publish workflow.


### From source

We use [poetry](https://python-poetry.org/) for dependency management and [PostgreSQL](https://www.postgresql.org/) 15
(12+ should be compatible) as a data storage (acquisition files are stored on the filesystem, not in the database).
To set up an instance with a demo database, follow these simple steps:

1. Create a Python virtual environment (`python -m venv venv`)
2. Enter the environment (`source venv/bin/activate`)
3. Install dependencies `poetry install`
4. Create a JWK (if you are unsure how, check this [mkjwk](https://mkjwk.org/) generator) and keep it private
5. Create an `.env` file according to `.env.example` (see the
   [Settings & Enumerations wiki](docs/catalog-wiki/Settings-&-Enumerations.md) for the full env-variable reference)
6. Execute migrations `python manage.py migrate`
7. Seed currencies and languages using `python manage.py setup`
8. Create a superuser using `python manage.py createsuperuser`
9. (Optional) Start a Celery worker (`celery -A evil_flowers_catalog worker -l info`) and Celery beat
   (`celery -A evil_flowers_catalog beat -l info`) to enable asynchronous tasks (OCR, LCP encryption, lifecycle sweeps,
   reservation queue, daily reminders, backups).

## Documentation

The OpenAPI specification is generated automatically from the source code using `python manage.py openapi`
(see `apps.openapi`) and is published from `master` at
[elvira.digital/EvilFlowersCatalog/](https://elvira.digital/EvilFlowersCatalog/).

Operator and contributor documentation lives in this repository under [`docs/catalog-wiki/`](docs/catalog-wiki/) and
is mirrored on the [GitHub Wiki](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/wiki). Start with:

- [Home](docs/catalog-wiki/Home.md) — overview and navigation
- [Settings & Enumerations](docs/catalog-wiki/Settings-&-Enumerations.md) — every environment variable
- [Storage](docs/catalog-wiki/Storage.md) — filesystem vs S3
- [Security](docs/catalog-wiki/Security.md) — authentication, authorization, JWT, LDAP
- [OPDS 2.0 Server](docs/catalog-wiki/OPDS-2.md)
- [Readium LCP Integration](docs/catalog-wiki/Readium-LCP-Integration.md) and
  [Deployment](docs/catalog-wiki/Readium-LCP-Deployment.md)
- [Management Commands](docs/catalog-wiki/Management-commands.md)
- [Asynchronous Tasks](docs/catalog-wiki/Asynchronous-Tasks.md)

Implementation proposals (`IP-XXX`) sit under [`docs/proposals/posts/`](docs/proposals/posts) — every feature has one.

## Acknowledgment

This open-source project is maintained by students and PhD candidates of the
[Faculty of Informatics and Information Technologies](https://www.fiit.stuba.sk/) at the Slovak University of
Technology. The software is utilized by the university, aligning with its educational and research activities. We
appreciate the faculty's support of our work and their contribution to the open-source community.

![](docs/images/fiit.png)

[BACKBONE, s.r.o.](https://www.backbone.sk/en/) actively supporting the development of this project by providing the required infrastrucutre, software licenses and working personel.

![](docs/images/backbone.svg)
