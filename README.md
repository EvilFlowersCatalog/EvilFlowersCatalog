# EvilFlowersCatalog

Simple books catalog compatible with [OPDS 1.2](https://specs.opds.io/opds-1.2) written in Python. Project also
includes simple REST API for catalog/feed management (basic CRUD operations).

**Work in progress**

## Install

We use [poetry]() for dependency management. We use [PostgreSQL]() to store data. To set up instance with demo database
follow these simple steps:

1. Create python virtual environment (`python -m venv venv`)
2. Enter environment (`source venv/bin/activate`)
3. Install dependencies `poetry install`
4. Create `.env` file according `.env.example`
5. Execute migrations `python manage.py migrate`
6. Load example database `python manage.py loaddata users.json api_keys.json`

Demo superuser name: `arthur.dent@backbone.sk`
Demo superuser password: `admin`

---
Made with ❤️ and ☕️ Jakub Dubec
