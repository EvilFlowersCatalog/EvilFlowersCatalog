# Changelog

## 0.4.0 : 2022-02-03

- **Feature**: Images and thumbnails
- **Feature**: Entry and categories are m:n
- **Change**: Load countries and languages from [pycountry](https://github.com/flyingcircusio/pycountry)
- **Feature**: Celery replaced by CRON jobs for periodic tasks
- **Feature**: Status endpoint `GET /api/v1/status`
- **Change**: Django 4.0

## 0.3.0 : 2021-08-30

- **Feature**: `Feed` can have multiple parents
- **Feature**: `OpdsView` introduced
- **Feature**: Public/Private `Catalog`
- **Feature**: OPDS facets
- **Feature**: Complete OPDS catalog acquisition feed
- **Change**: URL routes for OPDS

## 0.2.0 - 2021-08-26

- **Feature**: Extended CRUDs in RESTful API (authors, entries)
- **Feature**: Containerisation
- **Feature**: Acquisition uploads

## 0.1.0 - 2021-06-23

Initial minimum valuable product pre-release.

- **Architecture**: Multiple catalogs database design
- **Feature**: `Catalog`, `Feed` and `Entry` CRUD
- **Feature**: Simple OPDS catalog
- **Feature**: HTTP Authorization
