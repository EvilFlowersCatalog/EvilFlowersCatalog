# Evil Flowers Catalog

<p align="center">
    <img width="150" height="150" src="docs/images/logo.png">
</p>

A publication catalog server compatible with [OPDS 1.2](https://specs.opds.io/opds-1.2), written in Python with
a straightforward management REST API for CRUD operations.

## Features

We are aware that the current documentation may not be satisfactory, and we are actively working to improve it. Right
now there is available at least complete documentation for the REST endpoints using OpenAPI
here: [https://elvira.digital/EvilFlowersCatalog/](https://elvira.digital/EvilFlowersCatalog/) If you have any
questions regarding usage, feel free to
[open an issue for clarification](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/new?assignees=&labels=documentation%2C+help+wanted%2C+question&projects=&template=request-for-clarification.md&title=),
[start a discussion](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/discussions),
or [contact us directly](mailto:jakub.dubec@stuba.sk).

The current list of features:

- **OPDS 1.2**: Access your publications using [OPDS 1.2](https://specs.opds.io/opds-1.2) - suitable for e-book
  readers.
- **REST API**: Facilitates easy access and manipulation of catalog data for developers through CRUD operations.
- **Multiple storage options**: Store your documents on the filesystem path or S3 compatible storage.
- **Multi-tenant Catalog**: Supports multiple tenants, allowing separate catalogs for different user groups within the
  same server instance.
- **Authentication Support**: Offers both LDAP and local user authentication methods, ensuring secure access control.
- **Custom Feeds**: Users can organize publications into custom feeds, tailoring the catalog to specific needs or
  themes.
- **Publications Sharing**: Enables users to share publications with others, facilitating collaboration and
  distribution.
- **Annotation Storage**: Integrates with the EvilFlowersViewer project for storing annotations, enhancing the reading
  experience with personalized notes and highlights.
- **PDF Slicing & Editing**: Provides tools for slicing and editing PDF documents directly within the catalog, allowing
  for custom modifications and adjustments.
- **Asynchronous Task Processing with Celery**: EvilFlowers Catalog leverages a robust Celery-based distributed task
  system to efficiently handle resource-intensive and time-consuming jobs. This includes tasks like OCR processing,
  data extraction, and Readium package compression. By offloading these tasks to a scalable worker environment, the
  catalog ensures smooth and responsive user interactions while processing large datasets and files in the background.

The implementation is based on these RFCs:

- [RFC7807: Problem Details for HTTP APIs](https://datatracker.ietf.org/doc/html/rfc7807)
- [RFC7617: The 'Basic' HTTP Authentication Scheme](https://datatracker.ietf.org/doc/html/rfc7617)
- [RFC6705: The OAuth 2.0 Authorization Framework: Bearer Token Usage](https://datatracker.ietf.org/doc/html/rfc6750)
- [RFC5005: Feed Paging and Archiving](https://datatracker.ietf.org/doc/html/rfc5005) (not implemented yet)

## Work in progress

Although this project is already in use in a production environment, work is still in progress, and the API
remains unstable. If you wish to deploy the project, please feel free to open a discussion or
[send us an email](mailto:jakub.dubec@stuba.sk). We are actively working on new features and documenting existing
ones, but it takes time.

The main goal is to implement the complete [OPDS 1.2](https://specs.opds.io/opds-1.2) and later
[OPDS 2.0](https://drafts.opds.io/opds-2.0) specifications. The ordered list below represents the current progress:

- [ ] Readium DRM
- [ ] [Facets](https://specs.opds.io/opds-1.2#4-facets)
- [ ] [Search](https://specs.opds.io/opds-1.2#3-search)
- [ ] [Pagination](https://datatracker.ietf.org/doc/html/rfc5005)

## Installation

### Docker

A pre-built Docker image is available on the GitHub Container registry as
[evilflowerscatalog](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/pkgs/container/evilflowerscatalog).

The repository contains a working example of a `docker-compose.yml` file configured for a development environment.
You can use a similar configuration for production usage. The application image will be built from the source.

Setup steps (container name may differ):

1. Initialize containers `docker-compose up`
2. Import languages, currencies, and set up CRON jobs
   `docker exec -it evilflowerscatalog-django-1 python3 manage.py setup`
3. Create a superuser `docker exec -it evilflowerscatalog-django-1 python3 manage.py createsuperuser`

The server will start on port 8000.

### From source

We use [poetry](https://python-poetry.org/) for dependency management and [PostgreSQL](https://www.postgresql.org/) 15
(12+ should be compatible) as a data storage (acquisition files are stored on the filesystem, not in the database).
To set up an instance with a demo database, follow these simple steps:

1. Create a Python virtual environment (`python -m venv venv`)
2. Enter the environment (`source venv/bin/activate`)
3. Install dependencies `poetry install`
4. Create a JWK (if you are unsure how, check this [mkjwk](https://mkjwk.org/) generator) and keep it private
5. Create an `.env` file according to `.env.example`
6. Execute migrations `python manage.py migrate`
7. Import currencies, languages, and set up CRON jobs using `python manage.py setup`
8. Create a superuser using `python manage.py createsuperuser`

## Documentation

The OpenAPI specification is generated automatically from the source code using `python manage.py openapi` command
(check the `apps.openapi` for more). The complete documentation is available on
[https://elvira.digital/EvilFlowersCatalog/](https://elvira.digital/EvilFlowersCatalog/). Additional features are
detailed on the [GitHub Wiki](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/wiki).

## Acknowledgment

This open-source project is maintained by students and PhD candidates of the
[Faculty of Informatics and Information Technologies](https://www.fiit.stuba.sk/) at the Slovak University of
Technology. The software is utilized by the university, aligning with its educational and research activities. We
appreciate the faculty's support of our work and their contribution to the open-source community.

![](docs/images/fiit.png)

[BACKBONE, s.r.o.](https://www.backbone.sk/en/) actively supporting the development of this project by providing the required infrastrucutre, software licenses and working personel.

![](docs/images/backbone.svg)
