# Evil Flowers Catalog

<p align="center">
    <img width="150" height="150" src="docs/images/logo.png">
</p>

A e-book catalog server compatible with [OPDS 1.2](https://specs.opds.io/opds-1.2), written in Python with
a straightforward management REST API for CRUD operations.

[![Run in Insomnia}](https://insomnia.rest/images/run.svg)](https://insomnia.rest/run/?label=Evil%20Flowers%20Catalog%20API&uri=https%3A%2F%2Fgithub.com%2FSibyx%2FEvilFlowersCatalog%2Fblob%2Fmaster%2Fdocs%2FInsomnia_EvilFlowers.json)

## Features

**Work in progress**

The main goal is to implement the complete [OPDS 1.2](https://specs.opds.io/opds-1.2) and later
[OPDS 2.0](https://drafts.opds.io/opds-2.0) specifications. The ordered list below represents the current progress:

- [ ] [Previously Acquired Content](https://specs.opds.io/opds-1.2#61-relations-for-previously-acquired-content)
    - [ ] Shelves (`/opds/shelv`)
    - [ ] Subscriptions (`/opds/subscriptions`)
- [ ] [Facets](https://specs.opds.io/opds-1.2#4-facets)
- [ ] [Search](https://specs.opds.io/opds-1.2#3-search)
- [ ] [Pagination](https://datatracker.ietf.org/doc/html/rfc5005)
- [ ] [Complete acquisition feeds](https://specs.opds.io/opds-1.2#25-complete-acquisition-feeds)

The implementation is based on these RFCs:

- [RFC7807: Problem Details for HTTP APIs](https://datatracker.ietf.org/doc/html/rfc7807)
- [RFC7617: The 'Basic' HTTP Authentication Scheme](https://datatracker.ietf.org/doc/html/rfc7617)
- [RFC6705: The OAuth 2.0 Authorization Framework: Bearer Token Usage](https://datatracker.ietf.org/doc/html/rfc6750)
- [RFC5005: Feed Paging and Archiving](https://datatracker.ietf.org/doc/html/rfc5005) (not implemented yet)

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
(10+ should be compatible) as a data storage (acquisition files are stored on the filesystem, not in the database).
To set up an instance with a demo database, follow these simple steps:

1. Create a Python virtual environment (`python -m venv venv`)
2. Enter the environment (`source venv/bin/activate`)
3. Install dependencies `poetry install`
4. Create a JWK (if you are unsure how, check this [mkjwk](https://mkjwk.org/) generator) and keep it private
5. Create an `.env` file according to `.env.example`
6. Execute migrations `python manage.py migrate`
7. Import currencies, languages, and set up CRON jobs using `python manage.py setup`
8. Create a superuser using `python manage.py createsuperuser`

## Acknowledgment

This open-source project is maintained by students and PhD candidates of the
[Faculty of Informatics and Information Technologies](https://www.fiit.stuba.sk/) at the Slovak University of
Technology. The software is utilized by the university, aligning with its educational and research activities. We
appreciate the faculty's support of our work and their contribution to the open-source community.

![](docs/images/fiit.png)
