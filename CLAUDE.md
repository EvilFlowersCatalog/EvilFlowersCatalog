# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Evil Flowers Catalog is a Django-based OPDS (Open Publication Distribution System) 1.2 compatible publication catalog server. It provides a REST API for managing publications, catalogs, and users, with features like multi-tenant support, authentication, custom feeds, and PDF editing capabilities.

## Common Commands

### Development Setup
```bash
# Install dependencies using Poetry
poetry install

# Set up database and basic configuration
python manage.py migrate
python manage.py setup  # Import languages, currencies, set up CRON jobs
python manage.py createsuperuser
```

### Running the Application
```bash
# Development server
python manage.py runserver

# Run with Docker Compose
docker-compose up
```

### Database Operations
```bash
# Create and apply migrations
python manage.py makemigrations
python manage.py migrate

# Load/dump catalog data
python manage.py load_catalog <catalog_id> <file_path>
python manage.py dump_catalog <catalog_id> <file_path>
python manage.py purge_catalog <catalog_id>
```

### Code Quality
```bash
# Format code with Black
black .

# Generate OpenAPI documentation
python manage.py openapi
```

### Async Tasks
```bash
# Run Celery worker for background tasks
celery -A evil_flowers_catalog worker -l info

# Run Celery beat for scheduled tasks
celery -A evil_flowers_catalog beat -l info
```

## Architecture

### Django Apps Structure
- **apps/core/**: Core models (Entry, Catalog, User, etc.), authentication, and base functionality
- **apps/api/**: REST API endpoints with filters, forms, serializers, and views
- **apps/opds/**: OPDS 1.2 feed generation and catalog serving
- **apps/opds2/**: OPDS 2.0 implementation (in progress)
- **apps/files/**: File storage handling (filesystem and S3)
- **apps/readium/**: Readium DRM integration and license management
- **apps/tasks/**: Celery task definitions and management
- **apps/events/**: Event-driven architecture with Kafka/Celery executors
- **apps/openapi/**: OpenAPI documentation generation

### Key Models
- **Entry**: Publications/books with metadata, files, and relationships
- **Catalog**: Multi-tenant containers for organizing entries
- **User**: Authentication with local and LDAP support
- **Feed**: Custom organization of entries
- **Annotation**: User annotations and highlights
- **UserAcquisition**: User access permissions to entries

### Authentication
The system uses custom authentication backends:
- **BasicBackend**: HTTP Basic authentication
- **BearerBackend**: JWT token authentication
- **LDAP**: External LDAP authentication support

### Storage
Configurable storage backends:
- **FileSystemStorage**: Local filesystem storage
- **S3Storage**: S3-compatible object storage

### Background Processing
Celery is used for:
- OCR processing
- PDF manipulation
- Readium package encryption
- Data extraction tasks

## Configuration

Settings are environment-based with `.env` file support:
- **Database**: PostgreSQL with connection pooling
- **Cache**: Redis for session and application caching
- **Storage**: Configurable between filesystem and S3
- **Authentication**: JWT with RS256 signing
- **Readium**: LCP server integration settings

## Development Notes

- Uses Poetry for dependency management
- PostgreSQL 12+ required for database
- Redis required for caching and Celery broker
- Optional: Elasticsearch for search capabilities
- Optional: Kafka for event streaming
- Code formatting enforced with Black (119 character line length)
- All Django apps follow standard structure with filters/, forms/, serializers/, views/
- Custom management commands available in each app's management/commands/