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

## Proposal System

All feature development follows the **proposal-first methodology**:

1. Create proposal in `docs/proposals/posts/IP-XXX-feature-name.md`
2. Follow template: Status, Problem Statement, Proposed Solution, Implementation Plan, Alternatives, Trade-offs
3. Proposals use mkdocs-material blog format with metadata (draft, date, authors, categories, tags)
4. Accepted proposals become implementation specifications

**Proposal Template Structure**:
- Status, Problem Statement, Proposed Solution, Implementation Plan (phases with checkboxes)
- Technical Details, Alternatives Considered, Trade-offs and Risks, Success Criteria
- Future Considerations, References, Discussion, Changelog

**Writing Proposals - Important Guidelines**:

1. **No Time Estimates Required**: Do NOT include implementation time estimates or effort calculations. Focus on what needs to be done, not how long it will take. Users will decide scheduling.

2. **Always Update Changelog**: When making ANY changes to a proposal (including initial creation), update the Changelog table at the bottom with:
    - Date (YYYY-MM-DD format)
    - Author (username)
    - Brief description of changes

   Example:
   ```markdown
   ## Changelog

   | Date | Author | Changes |
   |------|--------|---------|
   | 2026-01-11 | jdubec | Initial draft based on IP-010, IP-011 insights |
   | 2026-01-12 | jdubec | Updated migration strategy for pre-production context |
   ```

3. **Implementation Plan**: Focus on concrete steps and phases, not timelines. Break work into actionable checkboxes without "this will take X hours" estimates.

4. **Update Proposals Index**: When creating or changing a proposal's status, update `docs/proposals/index.md` to reflect the current state in the proposals tracking table. This index provides a quick overview of all proposals and their states.

5. **Review Questions (AI-Created Proposals)**: When an AI agent creates a proposal draft, it MUST include a "Review Questions" section before the Changelog. This section identifies potential inconsistencies, edge cases, and unresolved technical decisions that require human input before implementation.

   **Workflow**:
    - Step 1: Create complete proposal draft with all standard sections
    - Step 2: Read back the created file to verify completeness
    - Step 3: Review the proposal for inconsistencies, contradictions, edge cases, and open questions
    - Step 4: Add "Review Questions" section with identified issues
    - Step 5: Update Changelog noting "Added Review Questions section"

   **Review Questions Format** (see POLY-012 for complete example):
   ```markdown
   ## Review Questions

   **Status**: ⏳ Awaiting Answers
   **Review Date**: YYYY-MM-DD
   **Reviewer**: Claude AI

   The following questions must be answered before implementation:

   ---

   ### Q1: [Question Title]

   **Issue**: [Description of the problem/inconsistency with line numbers]

   **Context**: [Why this matters]

   **Question**: [The specific question to answer]

   **Options**:
   - [ ] **A**: [Option description] (recommended if applicable)
   - [ ] **B**: [Option description]
   - [ ] **C**: [Option description]

   **Answer**:
   ```
   [User fills this in]
   ```

   **Resolution**:
   ```
   [AI writes this — describes how proposal will be updated based on the user's answer]
   ```

   ---
   ```

   **What to Review For**:
    - Schema/migration inconsistencies (e.g., nullable vs required fields)
    - Contradictions between sections (e.g., "optional" in schema, "required" in discussion)
    - Edge cases not handled (e.g., empty collections, NULL values)
    - Missing implementation details (e.g., "validation needed" without specifying logic)
    - Ambiguous statements (e.g., "inherited or set directly" without HOW)
    - Incomplete migration logic (e.g., data transformation missing steps)
    - Unresolved dependencies (e.g., references to other proposals)
    - Metadata inconsistencies (e.g., date conflicts)

   **Critical vs. Non-Critical Questions**:
    - Mark as 🔴 **Critical** if it blocks implementation or causes data loss
    - Mark as ⚠️ **Medium** if it affects user experience or performance
    - Mark as ℹ️ **Low** if it's a documentation/clarity issue

**Proposal Status Values**:
- **Draft**: Initial proposal, work in progress
- **Under Review**: Proposal complete, awaiting feedback/approval
- **Accepted**: Approved for implementation
- **Implemented**: Implementation complete
- **Rejected**: Proposal declined (with rationale in proposal)
- **Superseded**: Replaced by another proposal (reference new proposal)
