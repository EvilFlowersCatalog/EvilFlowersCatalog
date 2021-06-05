# Evil Flowers Catalog

Simple e-book catalog server compatible with [OPDS 1.2](https://specs.opds.io/opds-1.2) written in Python with simple
management REST API (basic CRUD operations).

[![Run in Insomnia}](https://insomnia.rest/images/run.svg)](https://insomnia.rest/run/?label=Evil%20Flowers%20Catalog%20API&uri=https%3A%2F%2Fgithub.com%2FSibyx%2FEvilFlowersCatalog%2Fblob%2Fmaster%2Fdocs%2FInsomnia_EvilFlowers.json)

## Features

**Work in progress**

The main goal is to implement complete [OPDS 1.2](https://specs.opds.io/opds-1.2)
and [OPDS 2.0](https://drafts.opds.io/opds-2.0) specification. Ordered list bellow represent current progress:

1. [ ] OPDS 1.2
    - [ ] Feeds and catalogs
        - [ ] Listing Acquisition Feeds
        - [ ] Complete Acquisition Feeds (`/opds/catalogs/:catalog.xml`)
        - [ ] Shelves (`/opds/shelves/:user.xml`)
    - [ ] Search
    - [ ] Facets
    - [ ] OPDS Catalog Entry Documents
    - [ ] Additional Link Relations
2. [ ] OPDS 2

Implementation is based on these RFCs:

- [RFC7807: Problem Details for HTTP APIs](https://datatracker.ietf.org/doc/html/rfc7807)
- [RFC7617: The 'Basic' HTTP Authentication Scheme](https://datatracker.ietf.org/doc/html/rfc7617)
- [RFC6705: The OAuth 2.0 Authorization Framework: Bearer Token Usage](https://datatracker.ietf.org/doc/html/rfc6750)

## Install

We use [poetry](https://python-poetry.org/) for dependency management and [PostgreSQL](https://www.postgresql.org/) 10+
as a store data. To set up instance with demo database follow these simple steps:

1. Create python virtual environment (`python -m venv venv`)
2. Enter environment (`source venv/bin/activate`)
3. Install dependencies `poetry install`
4. Create `.env` file according `.env.example`
5. Execute migrations `python manage.py migrate`
6. Load example database `python manage.py loaddata users.json api_keys.json`
7. You can import currencies and languages using `python manage.py basic_setup`

Default superuser name: `arthur.dent@example.com`
Default superuser password: `admin`

---
Made with ❤️ and ☕️ Jakub Dubec
