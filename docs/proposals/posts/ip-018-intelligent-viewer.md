---
draft: true
date: 2026-10-27
authors:
  - kordostomas
categories:
  - Feature
  - Architecture
tags:
  - viewer
  - extraction
  - figures
  - tables
  - math
  - highlights
  - assistant
  - docling
---

# IP-018: Intelligent Viewer

Every PDF uploaded to the catalog is analysed once, right after upload. Its figures, tables and
equations are found, cropped and described, and their exact position on the page is stored. The
reader's viewer can then show what the catalog knows about any of them, and the assistant can read
them. On top of that, the assistant can mark the sentences that answer a reader's question as an
**agent annotation**: an ordinary `Annotation` next to the reader's own, whose highlights also carry
their text, position and order, so they can be searched and stepped through.

<!-- more -->

## Status

**Status**: Draft
**Last Updated**: 2026-10-28
**Implementation**: Not started

This is a **parallel proposal**. It overlaps with [IP-012](ip-012-ai-powered-discovery.md),
[IP-013](ip-013-document-extraction-structure.md) and [IP-015](ip-015-assistant-fold-in.md), but it
does not wait for them and they do not wait for it. Where two proposals describe the same piece,
**whichever is implemented first owns it** and the other adapts to what shipped. See
[Relationship to other proposals](#relationship-to-other-proposals) for each overlap, including the
statements this proposal directly contradicts.

## Relationship to other proposals

### Parallel rule

- A shared piece (the extraction models, the service task contracts, the storage layout) is
  defined by whichever proposal implements it first.
- The later proposal updates its own text to build on what exists instead of re-specifying it, and
  logs that in its Changelog.
- Contradictions listed below are resolved in favour of this proposal **for the viewer use case
  only**. They do not change the goals of the other proposals.

### Overlaps and contradictions

| Proposal | What it says | What IP-018 says | Kind |
|---|---|---|---|
| IP-013 [Scope & non-goals](ip-013-document-extraction-structure.md#scope-non-goals) | "Explicit non-goal: read-time PDF highlight/anchoring … no per-passage highlight quads, no anchor resolver … and no new Viewer highlight render path." | Agent highlights are stored as positioned areas on the page and shown in the viewer. Quotes are resolved to page positions. | **Deprecated** (see below) |
| IP-013 [Review Q2](ip-013-document-extraction-structure.md#q2-read-time-anchoring-deferred-text-based-stored-bboxes-or-hybrid) | Answered "do NOT build monad-knowledge-style highlighting". | Builds exactly that, limited to agent highlight layers. | **Deprecated** (see below) |
| IP-013 [Scope & non-goals](ip-013-document-extraction-structure.md#scope-non-goals) | "Feature bounding boxes are kept only for cropping/display of figures/tables." | Feature positions are a first-class output: the viewer uses them to locate each figure, table and equation on the reader's page. | **Contradiction** (narrow) |
| IP-013 [Proposed Solution](ip-013-document-extraction-structure.md#proposed-solution) | `apps/extraction` runs on the IP-012 `discovery` worker image, folded into the catalog. | No extraction code is folded into the catalog. Extraction runs in the `evilflowers-*-service` repositories, each building its own image; the catalog compose only pulls pinned builds. | **Contradiction** (where the code lives) |
| IP-012 [Phase 1](ip-012-ai-powered-discovery.md#phase-1-event-driven-indexing-pipeline-replaces-61) | Retire `text-service` and `search-service`. | Both are **updated**, not retired: they stay in compose, are refactored in their own repositories and brought back to a running state. | **Contradiction** |
| IP-013 [Data Model Changes](ip-013-document-extraction-structure.md#data-model-changes) | Separate `extraction_figure`, `extraction_table`, `extraction_equation` tables. | One `extraction_region` table with a `kind`. Whichever lands first decides. | Overlap (schema) |
| IP-013 [Phase 2](ip-013-document-extraction-structure.md#phase-2-feature-extraction-docling-figures-tables-math-images) and [Phase 3](ip-013-document-extraction-structure.md#phase-3-classification-captioning) | Docling feature extraction, then VLM captions and classification. | Same tools and steps, but triggered on upload for the viewer. Sections, passages and subject classification stay with IP-013. | Overlap (shared work) |
| IP-015 [D1](ip-015-assistant-fold-in.md#d1-tool-set-exactly-three-read-only) | The chat assistant has exactly three read-only tools. | No tool-count limit (Q5); reading is bounded by a page budget instead. Region tools for the viewer context, and an agent that **writes** an annotation through its own endpoint. | **Contradiction** (tool set) |
| IP-015 [D12](ip-015-assistant-fold-in.md#d12-relationship-to-ip-012) | The assistant has no retrieval work. | Unchanged. IP-018 tools read one publication the reader has open, which is not catalog-wide retrieval. | Compatible |

### Deprecated: IP-013's highlighting non-goal

IP-013 ruled out read-time highlighting by pointing at monad-knowledge's `highlights.py` as the
machinery to avoid. That module has changed in monad-knowledge since IP-013 was resolved, so the
reasoning behind the non-goal no longer holds. **The non-goal and the IP-013 Q2 answer are
deprecated** as far as highlighting is concerned; IP-018 owns highlighting. IP-013 itself is
amended in a separate change (see [Q3](#q3-should-ip-013-be-amended-now-or-after-this-proposal-is-accepted)).

What changed in monad-knowledge (read 2026-10-28 at `988d0efdd`, `monad_knowledge/reading/highlights.py`,
IP-171 and IP-175 there): the old module matched whole paragraphs to claims by token overlap and
leaned on stored Neo4j bounding boxes. Since 2026-10-16 it is a deterministic, model-free
resolver: the extractor stores the exact evidence sentence of every claim as an offset into its
paragraph text, and at highlight time PyMuPDF finds that sentence on the page (`exact`), or most
of it in six-word windows (`partial`), or reports it `missed`. Over 63 papers it located 12,647 of
13,408 sentences (7,488 exact, 5,159 partial) and missed 761 (5.7 %). That is the machinery IP-018
reuses for agent highlights; see [Quote resolution](#quote-resolution).

## Problem Statement

A reader in the viewer sees a figure, a table or a formula as pixels. The catalog has no idea
what is on the page or where. Asking the assistant about a figure means describing it in words.
Asking it to "teach me X from this book" gets a summary, not a path through the book itself.

### Current state (verified 2026-10-27)

- **Extraction prototypes exist but are disconnected.** Seven `EvilFlowersCatalog/evilflowers-*-service`
  repositories hold extraction work:
    - `evilflowers-image-service`: pdfplumber crops, PaliGemma captions, ViT/ResNet labels, Kafka
      consumer writing to Elasticsearch. It computes each image's bounding box and then **discards
      it**, keeping only the page number. The caption model is loaded again for every image.
    - `evilflowers-equation-service`: Nougat page-to-LaTeX plus a regex. Region detection is a
      `TODO`, and the extractor only processes one page (`pages[1:2]`, marked "Remove after testing").
    - `evilflowers-analyzer-service`: PyMuPDF metadata and table of contents, Kafka orchestrator.
    - `evilflowers-video-service`: Whisper transcription. Out of scope.
    - `evilflowers-text-service`: Celery worker on queue `evilflowers_text_worker`. Chunks text and
      posts it to `search-service`. It extracts tables with pdfplumber and then **drops them**.
    - `evilflowers-search-service`: FastAPI over Milvus Lite and Elasticsearch.
    - `evilflowers-text-service-tp2025`: a fork of the text service.
- **The catalog only talks to two of them.** `apps/dataverse/services/text_publish.py` enqueues
  `evilflowers_text_worker.process_pdf` on every PDF upload (`apps/api/services/entry.py`), and
  `apps/api/services/search_service_client.py` lets OPDS 2.0 search query `search-service`. Both
  are in the development `compose.yml` only, which pulls `ghcr.io/evilflowerscatalog/evilflowers-text-service:main`
  and `ghcr.io/evilflowerscatalog/evilflowers-search-service:main`. Both images are out of date, and
  the containers are not running. Text-service already receives `CATALOG_API_URL` and
  `CATALOG_SECRET_KEY`, so a service writing back to the catalog over HTTP is an existing pattern.
- **The assistant can already read a reader's copy.** `apps/assistant/pdf.py` (`ReaderDocument`)
  rebuilds the viewer's page numbering over the original file, and `apps/assistant/explain.py`
  uses it with the `read_pdf_page` and `search_pdf` tools.
- **Annotations are already layers, but their content is only drawings.** An `Annotation` has a
  `title` and belongs to a `UserAcquisition`. The viewer treats it as a named group of drawings
  (`saveGroupFunc` / `getGroupsFunc`). Each `AnnotationItem` holds one page of that group as an SVG
  string in `content`. The `annotations` filter already limits non-superusers to their own
  annotations. `UserAcquisitionDownload` with `?annotations=true` draws the SVG `rect`, `line` and
  `circle` elements into the downloaded PDF (`apps/core/modifiers/pdf/annotations.py`). The catalog
  cannot search highlights, list them by text or point to one. The viewer has two layers, freehand
  **drawing** and manual **highlight**; this proposal adds the agent as the **third**. Nothing in the
  schema records which layer an annotation is. Page bookmarks are a further annotation, recognised
  only by the magic title `__page_bookmarks__`.

### Production state (2026-10-27)

`docker ps` on the production host lists only these containers:

| Container | Role |
|---|---|
| `evilflowers` | Catalog: gunicorn, Celery worker (default queue), Celery beat, event handler (`conf/supervisor.conf`) |
| `readium` | Readium LCP license server |
| `letsencrypt` | TLS termination |
| Flower | Celery monitoring dashboard |

**None of the `evilflowers-*-service` containers run in production**: not `text-service`, not
`search-service`, and not image, equation, analyzer or video. The `ocr-worker` and
`lcpencrypt-worker` from the development compose are not running either.

Consequences:

- Every PDF upload still enqueues `evilflowers_text_worker.process_pdf`, and nothing consumes that
  queue. Tasks are expected to accumulate in Redis. This needs checking in Flower.
- No production feature depends on any of the service repositories, so refactoring them, changing
  their task contracts or dropping Kafka breaks nothing in production.
- PostgreSQL and Redis are not containers on this host. Where they run is not yet recorded here.

Production storage is confirmed as `FileSystemStorage`, mounted from
`/opt/elvira-devops/environments/production/storage` to `/var/local/evilflowers`.

## Proposed Solution

### Overview

1. **Extract on upload.** When a PDF acquisition is stored, the catalog queues an extraction task
   for the service containers. They find every figure, table and equation, record its page and
   position, crop it to an image, and describe it: a caption and category for figures, markdown for
   tables, LaTeX for equations. Results are written back to the catalog, which owns the schema.
   The extraction code lives in the `evilflowers-*-service` repositories, not in the catalog.
2. **Expose it per reader.** An API returns the regions on a given page **in the reader's own page
   numbering**, only for pages the reader may see, with an authorized route for each crop.
3. **Give the assistant the same view.** The explain agent and the chat assistant, when opened for
   a publication, can list and read regions alongside page text.
4. **Agent annotations.** The assistant can answer a request by marking the relevant sentences in
   the book. The result is a regular `Annotation` of kind `agent`, holding the prompt and a short
   summary. Each highlight is an `AnnotationItem` with its text, position and order, plus an SVG in
   `content` generated by the server, so everything that already displays or exports annotations
   handles agent annotations unchanged.
5. **Backfill.** A command runs the same extraction for every PDF already in the catalog.

### Scope

**In scope**

- Figure, table and equation extraction with positions, on upload and as a backfill.
- Captions and categories for figures; markdown for tables; LaTeX for equations.
- Region and crop APIs scoped to a user's acquisition.
- Assistant tools for regions; explain accepts a region.
- Agent annotations: new fields on `Annotation` and `AnnotationItem`, generation, API.
- Storage, versioning and clean-up of extraction output.
- Refactoring the service repositories (image, equation, analyzer, text, search) in place: drop
  Kafka for Celery on the existing broker, keep positions, fix the known defects, publish images
  from each repository's own CI.
- Updating `text-service` and `search-service`, which are in compose but down and out of date, and
  running the whole compose end to end.
- Compose changes only on the catalog side: service entries pulling pinned image builds, in the
  development compose and the production compose.

**Out of scope**

- Any user interface. Viewer behaviour is prototyped separately and not specified here.
- Sections, passages, page labels and subject classification
  ([IP-013](ip-013-document-extraction-structure.md) Phases 1 and 3).
- Chunking, embeddings, search, recommendations ([IP-012](ip-012-ai-powered-discovery.md)).
- EPUB and other formats. PDF only.
- Folding any extraction code into the catalog repository or the catalog image.
- `evilflowers-video-service` and `evilflowers-text-service-tp2025`.
- Adding text and positions to existing reader annotations. They keep only their SVG.

### How it runs

**No extraction code is folded into the catalog.** Each piece of extraction stays in its own
`EvilFlowersCatalog/evilflowers-*-service` repository, is refactored there, and is published as an
image by that repository's CI to `ghcr.io/evilflowerscatalog/<repo>`. The catalog repository
changes only its compose files, which pull the correct, pinned build of each service.

| Repository | Role after the refactor |
|---|---|
| `evilflowers-analyzer-service` | Entry point for a document: opens the PDF, records page count and sizes, runs layout detection once and hands regions to the other services by kind (see [Q11](#q11-where-does-layout-detection-run)) |
| `evilflowers-image-service` | Figures: crop, size/aspect/coverage gates, caption and category. Keeps the bounding box |
| `evilflowers-equation-service` | Equations: LaTeX for each detected region, sympy normalization, crop |
| `evilflowers-text-service` | Text as today, plus tables: markdown, caption, crop and position instead of dropping them |
| `evilflowers-search-service` | Updated dependencies and image; stays the OPDS 2.0 search backend behind `SEARCH_SERVICE_URL` |

What the catalog keeps:

- The schema: `ExtractionRun`, `Region` and the annotation fields, in a small `apps/extraction`
  app with models, dispatch and ingestion only, no pipeline code.
- Dispatch: `app.send_task(...)` by task name and queue, exactly like
  `apps/dataverse/services/text_publish.py` does today.
- Ingestion: an internal endpoint the services post their results to, authenticated with a service
  credential (see [Q12](#q12-how-do-services-hand-results-back-to-the-catalog)).
- All reader-facing APIs, the assistant tools and agent annotations.

```yaml
# catalog compose (development and production): image references only
evilflowers-analyzer-service:
  image: ghcr.io/evilflowerscatalog/evilflowers-analyzer-service:<pinned tag>
  environment:
    REDIS_URL: redis://redis:6379/7
    STORAGE_PATH: /mnt/data
    CATALOG_API_URL: http://django:8000
    CATALOG_SERVICE_TOKEN: ${EXTRACTION_SERVICE_TOKEN}
  volumes:
    - <catalog storage>:/mnt/data
    - extraction-models:/models
# ...same shape for image-, equation-, text- and search-service
```

- Services talk to each other and to the catalog over Celery on the existing Redis broker. Kafka is
  removed from every service, because production does not run it.
- The catalog's own Celery worker does not listen on any service queue, so heavy work never runs
  next to gunicorn, and the catalog image gains no Docling or torch dependency.
- Services read the original PDF and write crops through the same storage mount as the catalog
  (`/var/local/evilflowers` in production). They run with the catalog's UID so the catalog can
  delete what they write.
- Model weights are kept in a volume, not in the images.
- Flower, already in production, shows every service queue and the backfill progress.
- The catalog records which service image tags produced a run (`provenance`), and
  `EVILFLOWERS_EXTRACTION_PIPELINE_VERSION` is bumped whenever the pinned tags change output.

## Implementation Plan

Work happens in the repository that owns it. Catalog phases change this repository; service phases
are done in each `evilflowers-*-service` repository, end with a published image, and touch the
catalog only through a compose image reference.

### Phase 1: Catalog schema, dispatch and ingestion (catalog repository)

- [ ] Create `apps/extraction` with `ExtractionRun` and `Region` models and migrations
      (see [Data Model Changes](#data-model-changes)). No pipeline code, no new dependencies.
- [ ] Storage layout `catalogs/<catalog>/<entry>/extraction/<acquisition_id>/v<N>/`, documented as
      the contract the services write to.
- [ ] Remove the acquisition's `extraction/` folder in `AcquisitionStorageService.delete`.
- [ ] Dispatch client next to `text_publish.py`: `send_task` to the entry service's queue with task
      id `extract:<acquisition_id>:v<pipeline_version>`, so duplicate sends are dropped. Creates the
      `ExtractionRun` with `status = queued`.
- [ ] Internal ingestion endpoint for runs and regions, authenticated with a service credential
      (mechanism per Q12). Rejects regions for runs that are not `running`.
- [ ] When every service has reported, make the run active in one transaction and delete the
      previous run's files.

### Phase 2: Service refactor (service repositories)

Common to every service:

- [ ] Replace Kafka consumers and producers with Celery workers on named queues over Redis.
- [ ] Read the original PDF and write crops through the shared storage mount, using the catalog's
      storage layout.
- [ ] Report results to the catalog ingestion endpoint; never write to the catalog database.
- [ ] Load models once per worker process, not per item.
- [ ] Failures on one page or one region are logged and skipped. A document never fails as a whole
      because of one bad region.
- [ ] CI builds and publishes a tagged image to `ghcr.io/evilflowerscatalog/<repo>`.

Per service:

- [ ] `evilflowers-analyzer-service`: open the PDF with PyMuPDF, record page count and page sizes,
      run the Docling layout pass (`do_ocr` only when a page has no text layer), and fan regions
      out by kind (subject to Q11).
- [ ] `evilflowers-image-service`: keep the bounding box it already computes instead of discarding
      it; convert to page fractions with a top-left origin; crop to WebP; apply gates (reject under
      80 px, extreme aspect ratios, anything covering over 90% of the page); captions and categories
      behind a setting so they can be switched off without a GPU (model per Q8).
- [ ] `evilflowers-equation-service`: implement region detection (currently a `TODO`), remove the
      `pages[1:2]` test limit, LaTeX per region, sympy normalization where it parses, crop.
- [ ] `evilflowers-text-service`: keep the tables it extracts (markdown, caption, crop, position)
      and report them as regions; keep text processing working against `search-service`.

### Phase 3: Text and search services back up

- [ ] `evilflowers-text-service`: update dependencies and base image, fix whatever keeps the
      container down, publish a new build.
- [ ] `evilflowers-search-service`: update dependencies and base image, fix whatever keeps the
      container down, publish a new build; `apps/api/services/search_service_client.py` keeps
      working against it.
- [ ] Catalog `compose.yml` (development): keep `:main` for every service (Q13) and add the
      analyzer, image and equation services.
- [ ] Run the full development compose end to end: upload a PDF, see the text task consumed, the
      extraction run reach `done`, regions and crops present, and OPDS 2.0 search answering.

### Phase 3b: Triggers and backfill (catalog repository)

- [ ] Queue extraction wherever a PDF acquisition file is stored: `apps/api/services/entry.py`
      next to the existing text-service publish, and `apps/dataverse/services/sync.py`.
- [ ] Re-queue when an acquisition's file is replaced.
- [ ] `manage.py backfill_extraction` with `--missing` (no active run), `--outdated` (older pipeline
      version), `--acquisition <id>` and a rate limit.

### Phase 4: Reader API

- [ ] Page mapping between original pages and reader pages, shared with
      `apps/assistant/pdf.py` instead of duplicated.
- [ ] `GET /api/v1/user-acquisitions/<id>/regions?page=N` and `GET .../regions/<region_id>`.
- [ ] `GET /data/v1/user-acquisitions/<id>/regions/<region_id>.webp` for the crop.
- [ ] Access checks identical to `UserAcquisitionDownload`: `check_user_acquisition_read` and the
      IP block. Regions on pages outside the acquisition's range are never returned.
- [ ] Extraction status per acquisition, so a client can tell "nothing found" from "not processed
      yet".

### Phase 5: Assistant access

- [ ] Tools `list_page_regions(page)` and `get_region(region_id)` for the explain agent and for
      chats opened on a user acquisition.
- [ ] `POST /api/v1/assistant/explain` accepts `region_id` as an alternative to selected text.
- [ ] Chats can be bound to a user acquisition, giving them `read_pdf_page`, `search_pdf` and the
      region tools for that publication only.

### Phase 6: Agent annotations

- [ ] Migration adding `kind`, `status`, `prompt`, `summary`, `model` and `unresolved_count` to
      `Annotation`. `kind` names the layer the annotation belongs to, matching the layers the
      viewer already has: `drawing` (freehand), `highlight` (manual text highlights) and `agent`
      (new). Existing rows get `drawing`. Rows titled `__page_bookmarks__`, which the portal uses as
      the page-bookmark container (`elvira-portal` `src/pages/common/Viewer.tsx`), get `bookmarks`.
- [ ] Migration adding nullable `seq`, `quads`, `text` and `note` to `AnnotationItem`. Agent items
      always fill them. Manual `highlight` items may fill `text` and `quads` from the text selection,
      which makes them searchable the same way.
- [ ] Highlight agent: reads the publication with the existing tools, returns exact quotes in
      reading order, each with the reader page it read it on, then a short summary built from those
      quotes.
- [ ] Quote gate: a quote is kept only if it occurs in the text `read_pdf_page` returned for its
      page, tolerant of whitespace only (monad-knowledge `locate_evidence`). A quote the model
      paraphrased is dropped here, before the PDF is searched, and counted in `unresolved_count`.
- [ ] Quote resolution on the original file, as described in [Quote resolution](#quote-resolution):
      `exact`, then `partial` six-word windows, else `missed`. Map the page to the reader's
      numbering, store the areas as page fractions and the method in `match`. Missed quotes are
      dropped and counted in `unresolved_count`. No model call.
- [ ] Port monad-knowledge's resolver tests (`tests/reading/test_span_highlights.py`: exact,
      one changed word is partial, absent is missed, a dry run writes nothing) against a small
      generated PDF.
- [ ] Generate `content` for each agent item: an SVG of `rect` elements, one per quad, in PDF
      points with a `viewBox` (Q7), so the existing PDF export draws it.
- [ ] One migration for all IP-018 schema: `extraction_run`, `extraction_region` (Q2), the
      annotation and item columns (Q9), and a data step converting existing reader SVG from screen
      pixels to PDF points (Q7). The viewer converts on save from then on.
- [ ] Highlight requests run as a Celery task; the annotation starts `pending` (Q4).
- [ ] Endpoint to request an agent annotation from a prompt. Listing, reading, renaming and
      deleting use the existing `annotations` and `annotation-items` endpoints, which return the
      new fields.
- [ ] Agent annotations: the reader may rename the title and delete one highlight or the whole
      annotation; every other update is rejected (Q10).
- [ ] Ownership is unchanged: `AnnotationFilter` already limits users to their own annotations.

### Phase 7: Production

- [ ] Add the analyzer, image, equation, text and search services to the production compose
      (`elvira-devops`), each pulling its pinned build and mounting production storage.
- [ ] Run the backfill, watching the queues in Flower.

### Prerequisites

- A PostgreSQL migration window for the new tables.
- A host for the service containers that can mount production storage.
- Push access to the `evilflowers-*-service` repositories and their `ghcr.io` packages.

## Technical Details

### Technology Stack

- **Docling** for layout, figures, tables and formulas in one pass (as chosen in
  [IP-013 Q1](ip-013-document-extraction-structure.md#q1-born-digital-feature-extractor-docling-unified-or-a-la-carte)).
- **PyMuPDF** for page geometry, cropping and quote resolution.
- **sympy** for LaTeX normalization.
- A local **vision-language model** for figure captions and categories. The same choice as
  IP-013 Phase 3 if that lands first.
- **Celery** on one named queue per service, broker as today. Kafka is removed from the services.
- **GitHub Actions + ghcr.io** in each service repository for image builds.
- The existing **Ollama** endpoint (`EVILFLOWERS_ASSISTANT_OLLAMA_*`) for the highlight agent.

### Coordinates

- Positions are stored against the **original file**: 1-based original page number, plus
  `x0, y0, x1, y1` as fractions of the page width and height, origin top-left.
- Fractions survive any zoom level. A top-left origin avoids the y-axis flip that
  `evilflowers-image-service` had to do by hand.
- The reader's copy differs from the original. `UserAcquisitionDownload` cuts it to
  `user_acquisition.range` and inserts the license page as page 2. The API converts every page
  number to the reader's numbering before returning it, using the same logic as
  `apps/assistant/pdf.py`.

### Storage

```
/var/local/evilflowers/catalogs/<catalog.url_name>/<entry.id>/
├── cover.jpg
├── thumbnail.jpg
├── <uuid>.pdf                     Acquisition.content
├── encrypted/                     Readium LCP
└── extraction/
    └── <acquisition.id>/
        └── v<N>/
            ├── p012-fig03.webp
            ├── p040-tab01.webp
            └── p041-eq02.webp
```

- Keyed by acquisition id, because all acquisitions of an entry share the entry folder.
- Only crops are files, written by the services. Positions, captions, markdown and LaTeX are posted
  to the catalog and live in PostgreSQL.
- A new version is written completely before it becomes active, so readers never see a partial run.
- Crops are never served as public media. They are parts of restricted publications.

### Data Model Changes

```sql
CREATE TABLE extraction_run (
    id               uuid PRIMARY KEY,
    acquisition_id   uuid REFERENCES acquisitions(id) ON DELETE CASCADE,
    pipeline_version int  NOT NULL,
    status           text NOT NULL,            -- queued | running | done | failed
    is_active        boolean DEFAULT false,    -- one active run per acquisition
    page_count       int,
    page_sizes       jsonb,                    -- [[width, height], ...] in PDF points
    error            text,
    started_at       timestamptz,
    finished_at      timestamptz,
    created_at       timestamptz DEFAULT now(),
    UNIQUE (acquisition_id, pipeline_version)
);

CREATE TABLE extraction_region (
    id          uuid PRIMARY KEY,
    run_id      uuid REFERENCES extraction_run(id) ON DELETE CASCADE,
    kind        text NOT NULL,                 -- figure | table | equation
    page        int  NOT NULL,                 -- original file, 1-based
    seq         int  NOT NULL,                 -- order on the page
    bbox        jsonb NOT NULL,                -- {x0, y0, x1, y1} as page fractions, top-left origin
    image_path  text,                          -- storage name of the crop
    caption     text,                          -- figure caption, or caption found near a table
    category    text,                          -- chart | photo | diagram | map | ...
    markdown    text,                          -- tables
    latex       text,                          -- equations
    provenance  jsonb                          -- model names, versions, generated_at
);

  -- Agent annotations reuse the existing tables (apps/core/models/annotation.py, annotation_item.py)
  ALTER TABLE annotations
      ADD COLUMN kind             text NOT NULL DEFAULT 'drawing', -- drawing | highlight | agent | bookmarks
      ADD COLUMN status           text,                          -- agent only: pending | done | failed
      ADD COLUMN prompt           text,                          -- agent only
      ADD COLUMN summary          text,                          -- agent only
      ADD COLUMN model            text,                          -- agent only
      ADD COLUMN unresolved_count int;                           -- agent only: quotes not found

  ALTER TABLE annotation_items
      ADD COLUMN seq   int,                                      -- agent only: reading order in the annotation
      ADD COLUMN match text,                                     -- agent only: exact | partial (see Quote resolution)
      ADD COLUMN quads jsonb,                                    -- agent only: [{x0, y0, x1, y1}, ...] page fractions
    ADD COLUMN text  text,                                     -- agent only: the highlighted sentence
    ADD COLUMN note  text;                                     -- agent only
-- page:    unchanged meaning, the reader's page number, as the viewer already stores it
-- content: still SVG; for agent items generated by the server from quads
```

### Quote resolution

Taken from monad-knowledge `monad_knowledge/reading/highlights.py` (`_locate`, `_page_index`),
which does the same job for claim evidence sentences. Two steps, kept apart:

1. **Text step (at answer time).** The agent's quote must occur in the page text it read, with
   whitespace differences only. This is monad-knowledge's extraction gate (`locate_evidence` in
   `extraction/claims.py`): a claim whose evidence is not literally in its paragraph is not stored,
   "because it cannot be checked against its source". Here it stops paraphrased quotes.
2. **PDF step (positions).** Each page of the original file gets one PyMuPDF text page built with
   `TEXT_DEHYPHENATE | TEXT_PRESERVE_WHITESPACE | TEXT_MEDIABOX_CLIP`, and its text lower-cased with
   whitespace collapsed. Then, per quote:
   - **exact**: the collapsed quote occurs in a page's text and `page.search_for(quote, quads=True,
     textpage=...)` returns quads. The quote's own page is tried first, then the rest of the
     acquisition's range (monad-knowledge scans every page; the agent's page is a cheap first guess).
   - **partial**: the quote is cut into six-word windows (windows under 20 characters dropped). The
     page holding the most windows wins if it holds at least half of them; each window found there
     is searched, and kept unless it returns more than three quads (ambiguous).
   - **missed**: neither works. Quotes under 20 characters are missed outright.

   Constants as in monad-knowledge: `_MIN_SPAN_CHARS = 20`, `_WINDOW_WORDS = 6`,
   `_MIN_WINDOW_CHARS = 20`, `_MAX_WINDOW_QUADS = 3`, `_MIN_WINDOW_SHARE = 0.5`.

What differs from monad-knowledge:

- **Output.** monad-knowledge writes PDF highlight annotations into an `_annotated/` copy with a
  legend page. IP-018 stores the quads as page fractions on `AnnotationItem` and generates the SVG
  `content` from them; the reader's file is never rewritten.
- **Page text.** monad-knowledge's evidence comes from GROBID text, which differs from PDF text in
  quotes, symbols and running heads, and is the main cause of its misses. IP-018's quotes come
  from `ReaderDocument`, which reads the same file with PyMuPDF (`get_text("text")`, no
  dehyphenation), so misses should be rarer. That is an expectation, not a measurement; the miss
  rate is reported (see Success Criteria). Hyphenated line breaks are the known gap: the reader
  text keeps `exam-\nple`, the PDF step's text page joins it, so the PDF step normalises both sides
  the same way (dehyphenate and collapse whitespace) before comparing.
- **Shared sentences.** monad-knowledge merges claims that share one evidence sentence into one
  highlight. Agent quotes are already one per item; two items with the same quote are merged and
  keep the lower `seq`.
- **Colours and attribution.** monad-knowledge colours by claim type and fades `cited` claims. The
  viewer colours by annotation (`AgentHighlightLayer`), so neither applies.

Region tables are new. Annotation tables keep their structure and only gain a `kind` with a default
and nullable columns, so
existing reader annotations, the viewer and the PDF export are unaffected. Table names are checked
against the catalog's `db_table` values during implementation.

### API Changes

```
# Regions (reader page numbers; pages outside the acquisition's range are never returned)
GET    /api/v1/user-acquisitions/{id}/regions?page=N
GET    /api/v1/user-acquisitions/{id}/regions/{region_id}
GET    /data/v1/user-acquisitions/{id}/regions/{region_id}.webp
GET    /api/v1/user-acquisitions/{id}/extraction            -> status, pipeline version, counts

# Explain
POST   /api/v1/assistant/explain                            -> accepts region_id

# Agent annotations
POST   /api/v1/user-acquisitions/{id}/agent-annotations     -> {prompt} -> Annotation (kind=agent)

# Existing endpoints, now returning the new fields
GET    /api/v1/annotations?user_acquisition_id=...          -> + kind, status, prompt, summary
GET    /api/v1/annotations/{id}
PUT    /api/v1/annotations/{id}                             -> title (the only change allowed on agent annotations)
DELETE /api/v1/annotations/{id}
GET    /api/v1/annotation-items?annotation_id=...           -> + seq, quads, text, note
```

### Assistant tools

| Tool | Reads or writes | Returns |
|---|---|---|
| `read_pdf_page` | read (exists) | Page text in reader numbering |
| `search_pdf` | read (exists) | Pages and snippets for a term |
| `list_page_regions(page)` | read | Regions on a reader page: id, kind, caption or first line |
| `get_region(region_id)` | read | Caption and category, markdown, or LaTeX. The crop for a model that accepts images |

All tools are bound to one user acquisition and inherit its access check and page range.

### Configuration

```
# catalog
EVILFLOWERS_EXTRACTION_ENABLED=1
EVILFLOWERS_EXTRACTION_PIPELINE_VERSION=1          # bumped when pinned service tags change output
EVILFLOWERS_EXTRACTION_QUEUE=evilflowers_analyzer  # entry service queue
EVILFLOWERS_EXTRACTION_SERVICE_TOKEN=...           # credential the services present on ingestion

# services (set in compose)
REDIS_URL=redis://redis:6379/7
STORAGE_PATH=/mnt/data
CATALOG_API_URL=http://django:8000
CATALOG_SERVICE_TOKEN=...
CAPTIONS_ENABLED=1                                 # image-service; off on CPU-only hosts
CAPTION_MODEL=...
HF_HOME=/models
```

## Alternatives Considered

### Alternative 1: Run the existing service repositories unchanged

**Pros:** no work in the services. **Cons:** none of them run today, image-service throws away
positions, equation-service never finds regions, and they speak Kafka, which production does not
run. **Not chosen.** The repositories are kept, but refactored (Phase 2).

### Alternative 2: Fold extraction into the catalog as `apps/extraction`, run from the catalog image

This was the original IP-018 design (and IP-013's): extraction code in the catalog repository, a
second container from the same image on an `extraction` queue, writing through the Django models.

**Pros:** one build, one schema, no write-back API. **Cons:** Docling and torch in the catalog image,
extraction releases tied to catalog releases, and the existing service repositories thrown away.
**Not chosen** at review on 2026-10-28: images are built in their own repositories and the catalog
compose only pulls them.

### Alternative 3: Extract on demand when a reader opens a page

**Pros:** no backfill, no storage for unopened books. **Cons:** seconds to minutes of GPU time per
page while the reader waits, and the assistant would have nothing to read for unopened pages.
**Not chosen.**

### Alternative 4: Separate `highlight_layer` and `highlight` tables

**Pros:** agent data kept apart from reader drawings; no nullable columns. **Cons:** a second
"named layer per user acquisition" concept next to `Annotation`, which already is one. It also
needs its own endpoints, its own ownership filter, its own path through the PDF export, and its own
handling in the viewer. **Not chosen.** `Annotation` and `AnnotationItem` are extended instead.

### Alternative 4b: Agent highlights as SVG only

**Pros:** no schema change at all. **Cons:** SVG cannot be searched, ordered or navigated, and
carries no text. **Not chosen.** The SVG is kept for display and export, and the structured columns
sit next to it.

### Alternative 5: Text offsets instead of page positions for highlights

**Pros:** independent of layout. **Cons:** pdf.js and PyMuPDF split text differently, so offsets
drift between server and viewer. **Not chosen.** Page positions are what the viewer needs anyway.

## Trade-offs and Risks

### Trade-offs

- **Several repositories, builds and deploys** and an ingestion API, in exchange for a catalog image
  without ML dependencies and services that release on their own.
- **Extraction at upload** costs storage and compute for books nobody opens, in exchange for
  instant results in the viewer.
- **Positions against the original file** require a page mapping on every response, in exchange
  for extraction that is valid for every reader regardless of their page range.

### Risks

| Risk | Impact | Mitigation |
|---|---|---|
| No GPU on the production host | High | Captions behind a setting; Docling layout runs on CPU; backfill paced |
| Docling memory use on large scanned books | Medium | Concurrency 1; page batches; skip OCR when a text layer exists |
| False figures on scans (stains, decorations) | Medium | Size, aspect and page-coverage gates; categories |
| Agent quotes not found verbatim | Medium | Whitespace and hyphenation normalization before search; `unresolved_count` stored on the annotation |
| Agent item SVG and quads disagree | Medium | Server generates SVG from quads; agent annotations are read-only |
| Reader page mapping differs from the downloaded file | High | One shared mapping used by downloads, regions and the assistant; tests over ranged acquisitions |
| Upload queue grows during backfill | Low | Separate queues; rate-limited command; Flower |
| Service output drifts from the catalog schema | High | Ingestion endpoint validates payloads; pinned image tags; `provenance` records the tags; pipeline version bumped with tag changes |
| A service is down and a run never completes | Medium | Run stays `running` and is visible in extraction status; backfill `--outdated`/`--missing` re-queues; timeout marks it `failed` |
| Crops written with a different UID than the catalog | Medium | Services run with the catalog's UID; checked in the end-to-end run |
| IP-012 plans to retire text- and search-service | Low | Recorded in [Overlaps and contradictions](#overlaps-and-contradictions); IP-012 adapts to what ships |

## Success Criteria

- [ ] Every newly uploaded PDF gets an extraction run without blocking the upload.
- [ ] Every existing PDF has an active run after the backfill.
- [ ] For any page a reader can see, the API returns its figures, tables and equations with
      positions in that reader's page numbering.
- [ ] A reader without access to a publication cannot get its regions or crops, and cannot see
      another user's annotations.
- [ ] The explain agent can explain a region given only its id.
- [ ] A highlight request produces an `Annotation` of kind `agent` with a summary, whose items are
      ordered, positioned on the correct reader pages, and carry their text.
- [ ] Quote resolution is reported per annotation (exact, partial, missed) and over a sample of
      publications, as monad-knowledge reports it; its 5.7 % miss rate on GROBID text is the
      reference, not a bar.
- [ ] Agent annotations appear through the existing annotation endpoints and in PDF downloads with
      `?annotations=true`, with no change to how reader annotations behave.
- [ ] Deleting an acquisition removes its extraction files.
- [ ] The catalog image has no extraction dependencies; the catalog repository only changed its
      compose files to run the services.
- [ ] Every service image is built in its own repository and pulled by a pinned tag.
- [ ] The full development compose, including text- and search-service, runs end to end.

## Future Considerations

- Filling `text` and `quads` for reader highlights too, so they can be searched like agent ones.
- Sharing an annotation with another reader of the same publication.
- Region search across the catalog once IP-012 search exists.
- EPUB support.
- Alt text for accessibility from figure captions.

## References

- [IP-012: AI-Powered Discovery](ip-012-ai-powered-discovery.md)
- [IP-013: Document Extraction, Classification & Feature Pipeline](ip-013-document-extraction-structure.md)
- [IP-015: In-Catalog Chat Assistant](ip-015-assistant-fold-in.md)
- Catalog: `apps/assistant/explain.py`, `apps/assistant/pdf.py`, `apps/files/views.py`,
  `apps/files/services.py`, `apps/api/services/entry.py`, `apps/dataverse/services/text_publish.py`,
  `apps/core/models/annotation.py`, `apps/core/models/annotation_item.py`,
  `apps/core/modifiers/pdf/annotations.py`, `apps/api/filters/annotations.py`, `conf/supervisor.conf`,
  `apps/api/services/search_service_client.py`, `compose.yml`
- Viewer: `EvilFlowersViewer` `src/components/pdf-viewer-page/pdf-edit/EditCanvas.vue` (`addHighlight`),
  `src/assets/utils/interfaces.ts` (`IEditPackage`)
- Service repositories: `EvilFlowersCatalog/evilflowers-image-service`, `-text-service`,
  `-equation-service`, `-analyzer-service`, `-search-service`, `-video-service`,
  `-text-service-tp2025`
- Docling, PyMuPDF (`Page.search_for`), sympy
- monad-knowledge (`Sibyx/monad-knowledge`, private; read at `988d0efdd`):
  `monad_knowledge/reading/highlights.py` (resolver), `monad_knowledge/extraction/claims.py`
  (`locate_evidence`), `tests/reading/test_span_highlights.py`, proposals IP-171 (evidence spans)
  and IP-175 (web reader layers anchored by quote with prefix and suffix)

## Review Questions

**Status**: ✅ Answered 2026-10-28, except the caption model inside Q8 (decided in Phase 2).
Q2, Q4, Q7 and Q9 are the schema questions, so IP-018 needs one migration.
**Review Date**: 2026-10-27
**Reviewer**: Claude AI

The following questions must be answered before implementation:

---

### Q1: 🔴 Where do the extraction services run, and do they have a GPU?

**Issue**: [How it runs](#how-it-runs) assumes the service containers mount production storage.
Production has four containers and no confirmed GPU. IP-013 tracks T4 placement as an open item (#65).

**Context**: Without a GPU, Docling layout still works but figure captions and formula recognition
become slow enough to stall the backfill.

**Question**: Where do the extraction service containers run?

**Options**:
- [x] **A**: On the production host, CPU only, captions switched off until a GPU is available (recommended)
- [ ] **B**: On the T4 machine, mounting production storage over the network
- [ ] **C**: On the production host after a GPU is added

**Answer**:
```
For now the production host on CPU. Once a machine with a GPU exists the services move there;
that is a simple change.
```

**Resolution**:
```
Services run on the production host, CPU only. Captions are behind a setting and off until a GPU
host exists (Phase 2). Moving a service is a compose change: its image, storage mount and the
catalog URL. Phase 7 no longer has a GPU decision step.
```

---

### Q2: 🔴 One region table or IP-013's three feature tables?

**Issue**: [Data Model Changes](#data-model-changes) defines `extraction_region` with a `kind`.
IP-013 defines `extraction_figure`, `extraction_table` and `extraction_equation`.

**Context**: The parallel rule says the first implementation wins. Choosing now avoids a later
migration.

**Question**: Which schema should the first implementation use?

**Options**:
- [x] **A**: One `extraction_region` table, and IP-013 adopts it (recommended)
- [ ] **B**: IP-013's three tables, and IP-018 adapts

**Answer**:
```
One extraction_region table (option A).
```

**Resolution**:
```
The Data Model keeps a single `extraction_region` table with `kind`. It is created in the one
IP-018 migration (see Q7), together with `extraction_run` and the annotation columns, and
stays empty until the services post results. IP-013 is amended to adopt it (Q3).
```

---

### Q3: ⚠️ Should IP-013 be amended now or after this proposal is accepted?

**Issue**: IP-013 still lists highlighting as a non-goal, and its Q2 answer rejects it. This
proposal contradicts both.

**Context**: Two Draft proposals with opposite statements will confuse anyone implementing either.

**Question**: How should IP-013 reflect this?

**Options**:
- [ ] **A**: Add a note and Changelog entry to IP-013 pointing here, leave its body unchanged (recommended)
- [x] **B**: Edit IP-013's non-goal to exclude the viewer use case
- [ ] **C**: Leave IP-013 untouched until one of the two is accepted

**Answer**:
```
Deprecate the explicitly disallowed highlighting in IP-013. It was based on monad-knowledge's
highlights.py, which has changed since.
```

**Resolution**:
```
Added "Deprecated: IP-013's highlighting non-goal" under Relationship to other proposals, and
re-labelled the two IP-013 highlighting rows in the overlaps table from Contradiction to Deprecated.
IP-013's non-goal, its Alternative 4 and its Q2 answer are to be marked deprecated in IP-013 itself
in a separate change; this update touches IP-018 only.
```

---

### Q4: ⚠️ Is the highlight agent synchronous or a background task?

**Issue**: [Phase 6](#phase-6-agent-annotations) does not say how long a request may take. The
agent may read many pages before answering.

**Context**: `explain` is synchronous today and bounded by `MAX_TOOL_ROUNDS = 8`. A "teach me X from
this book" request needs far more reading than that.

**Question**: How is a highlight request executed?

**Options**:
- [x] **A**: Background Celery task on the default queue; the annotation is created with `status = pending` and filled in (recommended)
- [ ] **B**: Synchronous request with a higher tool-round limit
- [ ] **C**: Streamed over SSE like chat messages (IP-015 D5)

**Answer**:
```
Background Celery task (option A).
```

**Resolution**:
```
A highlight request creates the `Annotation` with `status = pending` and enqueues a Celery
task on the default queue; the task fills summary and items and sets `done` or `failed`. The
viewer already shows pending and failed annotations. `annotations.status` stays in the schema.
```

---

### Q5: ⚠️ Does this override IP-015 D1's three-tool limit?

**Issue**: IP-015 [D1](ip-015-assistant-fold-in.md#d1-tool-set-exactly-three-read-only) fixes the
chat assistant at three read-only tools. This proposal adds two region tools for chats bound to a
publication, and an agent that writes annotations.

**Context**: The highlight agent writes data, which D1 rules out for chat.

**Question**: How do the two proposals coexist?

**Options**:
- [ ] **A**: D1 applies to catalog-wide chat only; chats bound to a user acquisition get the reader tools, and annotations are written only through the agent-annotation endpoint (recommended)
- [ ] **B**: Amend D1 to allow these tools everywhere
- [ ] **C**: Keep region tools to the explain agent only

**Answer**:
```
No tool limit is needed; tool calls can be unlimited. PDF reads must be limited: the agent must
not read all 200 pages.
```

**Resolution**:
```
IP-015 D1's three-tool limit is dropped for the assistant (IP-015 to be amended). Chats bound to a
reader's copy get the reader and region tools; annotations are still written only through the
agent-annotation endpoint. What is bounded is reading: a page budget per chat turn, explain call
and highlight run (`EVILFLOWERS_ASSISTANT_MAX_PAGES_READ`). A page already read in the request is
free to read again; past the budget `read_pdf_page` returns an error telling the model to answer
from what it read. `search_pdf` does not count. A round limit stays only as a runaway guard,
well above what the page budget allows (Implemented in the catalog for chat and explain,
2026-10-28.)
```

---

### Q6: ⚠️ Should the dead text-service queue be stopped?

**Issue**: [Production state](#production-state-2026-10-27) shows every upload enqueues
`evilflowers_text_worker.process_pdf` with no consumer.

**Context**: Tasks accumulate in Redis. IP-012 already plans to retire text-service.

**Question**: What happens to the publish call?

**Options**:
- [ ] **A**: Put it behind a setting that is off in production, as part of Phase 3 here (recommended)
- [ ] **B**: Remove it now, outside any proposal
- [ ] **C**: Leave it until IP-012 replaces it
- [x] **D**: Bring text-service back so the queue has a consumer

**Answer**:
```
Update text-service and search-service. Both are down but still in compose, and both are old.
Refactor everything and try running it.
```

**Resolution**:
```
The publish call stays. Phase 3 ("Text and search services back up") updates both services in
their own repositories, publishes new builds, pins them in compose and runs the full development
compose end to end. Production gets both services in Phase 7, which gives the queue a consumer.
The IP-012 row in the overlaps table now records this as a contradiction (IP-012 retires them).
```

---

### Q7: 🔴 Which coordinate system does annotation SVG use?

**Issue**: [Phase 6](#phase-6-agent-annotations) generates SVG for agent items, but the existing
annotation SVG has no agreed coordinate system. The viewer's `addHighlight` (`EditCanvas.vue`)
writes `rect` positions in **screen pixels at the current zoom**, relative to the page overlay. The
PDF export (`apps/core/modifiers/pdf/annotations.py`) reads the same numbers as **PDF points** and
ignores `opacity`.

**Context**: Reader highlights drawn at any zoom other than 100% land in the wrong place in an
exported PDF today, and they may shift in the viewer after a zoom change unless it rescales them.
Agent SVG has to match whatever convention is chosen, or agent highlights will be off in either the
viewer or the export.

**Question**: What coordinates does `AnnotationItem.content` use?

**Options**:
- [x] **A**: PDF points of the page, with a `viewBox` on the SVG root; the viewer scales on render and converts on save (recommended)
- [ ] **B**: Page fractions (0–1) with `viewBox="0 0 1 1"`; the PDF export multiplies by page size
- [ ] **C**: Keep screen pixels and store the zoom alongside; agent SVG is generated at 100%

**Answer**:
```
PDF points with a viewBox on the SVG root (option A).
```

**Resolution**:
```
`AnnotationItem.content` is SVG in PDF points of the page, `viewBox="0 0 <width> <height>"`.
The PDF export reads it unchanged. The viewer scales it on render and converts screen pixels
to points on save. Agent SVG is generated from `quads` times the page size. Existing reader
items are converted in a data step of the same IP-018 migration, so there is one migration:
region tables, annotation columns and the SVG conversion together.
```

---

### Q8: ℹ️ Which caption model?

**Issue**: IP-013 names Qwen2-VL, and image-service used PaliGemma. This proposal leaves it as a
setting.

**Context**: The assistant already uses an Ollama endpoint, which can serve vision models.

**Question**: Where do captions come from?

**Options**:
- [ ] **A**: The existing Ollama endpoint with a vision model, so no model runs inside image-service (recommended)
- [ ] **B**: Qwen2-VL loaded inside image-service (replacing PaliGemma)
- [ ] **C**: Docling's built-in picture description

**Answer**:
```
Captions belong to the vision image-service, but a multimodal LLM is preferred. Not decided which.
```

**Resolution**:
```
Captioning stays in image-service and is done by a multimodal LLM, not PaliGemma or the ViT/ResNet
classifiers. Which model, and whether image-service calls it through the existing Ollama endpoint
or loads it itself, is decided in Phase 2 by running both on a sample of figures from the catalog.
On the CPU host (Q1) the Ollama route is the only practical one. Still open: the model.
```

---

### Q9: ⚠️ One annotation item per highlight, or one per page?

**Issue**: The viewer loads one item per page per annotation (`getLayerFunc(page, groupId)` in
`IEditPackage`). [Phase 6](#phase-6-agent-annotations) gives each agent highlight its own `seq`,
`text` and `quads`.

**Context**: One item per highlight keeps each highlight addressable and navigable. One item per
page matches what the viewer loads today but needs a list of highlights inside a single item.

**Question**: How are agent highlights split into items?

**Options**:
- [x] **A**: One item per highlight; clients accept several items per page for an annotation (recommended)
- [ ] **B**: One item per page, with `quads` and `text` holding a list of highlights and `seq` per highlight inside it

**Answer**:
```
One item per highlight (option A).
```

**Resolution**:
```
Each agent highlight is its own `AnnotationItem` with its own `seq`, `match`, `text` and
list of `quads`. The viewer's agent layer already loads a whole annotation, so
several items per page per annotation are expected; the drawing layer keeps one item per page.
```

---

### Q10: ℹ️ Can a reader edit an agent annotation?

**Issue**: [Phase 6](#phase-6-agent-annotations) makes agent annotations read-only apart from the
title, so the generated SVG never disagrees with `quads` and `text`.

**Context**: A reader may want to draw on top of what the agent marked.

**Question**: What can a reader change?

**Options**:
- [x] **A**: Only the title; to change it, the reader copies it into a new annotation of kind `highlight` (recommended)
- [ ] **B**: Anything; edits clear `quads` and `text` on the changed item

**Answer**:
```
Readers can only delete it, and rename the annotation (group) title. Each highlight stays exactly
where the agent put it, with everything that belongs to it. Editing the description may come
later; for now it is agent -> database, and the reader can delete, as the viewer already does.
```

**Resolution**:
```
Agent annotations: the reader may rename the title, delete one highlight or delete the whole
annotation (the viewer's `deleteHighlightFunc` and `deleteAnnotationFunc`). Every other update
to an agent annotation or its items is rejected. No copy-to-highlight path. Editing the summary
is a future consideration.
```

---

### Q11: 🔴 Where does layout detection run?

**Issue**: [How it runs](#how-it-runs) gives figures, equations and tables to three different
services. Docling finds all three in one layout pass, which is the expensive part on CPU.

**Context**: Running layout in each service triples the cost per document. Running it once creates
a dependency between services.

**Question**: Which service finds the regions?

**Options**:
- [x] **A**: analyzer-service runs Docling layout once and sends each region to the service for its kind; image, equation and text services only enrich (recommended)
- [ ] **B**: Each service runs its own detection for its own kind; the catalog dispatches to all three in parallel
- [ ] **C**: One new `evilflowers-extraction-service` repository replaces analyzer, image and equation services

**Answer**:
```
analyzer-service runs Docling (option A).
```

**Resolution**:
```
analyzer-service runs Docling layout once per document and sends each region to the service
for its kind; image, equation and text services only enrich. Docling layout runs on CPU (Q1).
```

---

### Q12: 🔴 How do services hand results back to the catalog?

**Issue**: The catalog owns `ExtractionRun` and `Region`, but no service may write to its database
directly.

**Context**: text-service already has `CATALOG_API_URL` and `CATALOG_SECRET_KEY`. Sharing the
catalog's Django `SECRET_KEY` with every service is broader than needed.

**Question**: What is the write-back channel and credential?

**Options**:
- [ ] **A**: HTTP POST to an internal catalog endpoint with a dedicated service token (`EVILFLOWERS_EXTRACTION_SERVICE_TOKEN`), not the Django secret (recommended)
- [ ] **B**: Services send a Celery task to a catalog queue (`catalog.ingest_regions`), and the catalog worker writes the rows
- [ ] **C**: HTTP POST authenticated with the existing `CATALOG_SECRET_KEY`

**Answer**:
```
Use evilflowers-protocol (https://github.com/EvilFlowersCatalog/evilflowers-protocol), which is
meant for communication between containers.
```

**Resolution**:
```
evilflowers-protocol holds the catalog's OpenAPI spec, synced from the catalog's `docs.yml` on
every deploy; a push there regenerates the `evilflowers-protocol-python` client. So services
hand results back over HTTP to catalog endpoints that are part of the catalog's own API, called
through the generated Python client, authenticated with the catalog's bearer JWT (an API key of
a dedicated service user), not the Django `SECRET_KEY`. text-service's `CATALOG_SECRET_KEY` auth
moves to the same API key. The ingestion endpoints must be in the OpenAPI spec so the client
gets them. The synced spec is at 0.13.0 (2026-03-31), so the client is regenerated after Phase 1.
```

---

### Q13: ⚠️ What does "the correct build" mean in compose?

**Issue**: `compose.yml` pulls `:main` for text- and search-service. A moving tag can change output
without the catalog's `pipeline_version` changing.

**Context**: The catalog records provenance per run and decides when to re-extract by version.

**Question**: How are service images referenced?

**Options**:
- [ ] **A**: Semver release tags from each repository's CI, pinned in both composes; bumping a tag that changes output also bumps `EVILFLOWERS_EXTRACTION_PIPELINE_VERSION` (recommended)
- [x] **B**: `:main` in development, release tags in production
- [ ] **C**: Image digests (`@sha256:...`) everywhere

**Answer**:
```
`:main` in development, release tags in production (option B).
```

**Resolution**:
```
Development compose pulls `:main` of every service, so a merged change reaches dev on the next
pull. Production pins release tags; bumping a tag that changes output bumps
`EVILFLOWERS_EXTRACTION_PIPELINE_VERSION`. `provenance` records the image tag and digest, so a
dev run on a moving `:main` can still be traced.
```

---

### Q14: ℹ️ What does "updating" search-service cover?

**Issue**: Phase 3 brings search-service back up. It is a FastAPI service over Milvus Lite and
Elasticsearch, and IP-012 plans to replace it.

**Context**: Only OPDS 2.0 search uses it through `search_service_client.py`.

**Question**: How far does the update go?

**Options**:
- [x] **A**: Dependencies, base image and whatever is needed to run and pass the client's calls; no new features (recommended)
- [ ] **B**: Also add region search (figure captions, table markdown) to it
- [ ] **C**: Also move it off Milvus Lite to the store IP-012 picks

**Answer**:
```
Run as-is, no features (option A).
```

**Resolution**:
```
search-service gets dependencies, base image and whatever the catalog client needs to run; no
new features, Milvus Lite stays.
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-10-27 | kordostomas | Initial draft: extraction on upload, reader-scoped regions, assistant region tools, agent highlight layers; parallel to IP-012/013/015 with overlaps and contradictions listed; logged that no `evilflowers-*-service` container runs in production |
| 2026-10-27 | kordostomas | Added Review Questions section |
| 2026-10-27 | kordostomas | Dropped `highlight_layer`/`highlight`: agent highlights extend `Annotation` and `AnnotationItem` with nullable columns and server-generated SVG in `content`; existing annotation endpoints reused; replaced Q7 (ownership already enforced by `AnnotationFilter`) with the SVG coordinate question; added Q9 (item per highlight or per page) and Q10 (editing agent annotations) |
| 2026-10-27 | kordostomas | Aligned `Annotation.kind` with the viewer's layers: `drawing`, `highlight`, `agent` (third layer), plus `bookmarks` replacing the `__page_bookmarks__` title convention; manual highlights may also fill `text` and `quads` |
| 2026-10-28 | kordostomas | Review answers: IP-013's highlighting non-goal deprecated (monad-knowledge `highlights.py` has changed); no fold-in of extraction code — services are refactored in their own `evilflowers-*-service` repositories, images built there, catalog only changes compose to pull pinned builds; text- and search-service updated instead of retired and the full compose run end to end. Rewrote How it runs, Phases 1–3 and 7, Alternatives 1–2, risks, configuration and success criteria; answered Q3 and Q6; added Q11 (layout detection placement), Q12 (result write-back), Q13 (image pinning), Q14 (search-service update scope) |
| 2026-10-28 | kordostomas | Highlighting after monad-knowledge `988d0efdd`: described what its `highlights.py` does now; added Quote resolution (text gate, then exact / partial six-word windows / missed with its constants, and the differences), `AnnotationItem.match`, the quote gate and test port in Phase 6, a resolution-rate success criterion and references |
| 2026-10-28 | kordostomas | Answered the schema questions: Q2 one `extraction_region` table, Q4 background Celery task, Q7 SVG in PDF points with a viewBox (existing reader SVG converted), Q9 one item per highlight; Phase 6 now has one migration for all IP-018 schema |
| 2026-10-28 | kordostomas | Answered the rest: Q1 production host on CPU, move when a GPU host exists; Q5 no tool-count limit, a page budget per request instead (implemented for chat and explain); Q8 captions by a multimodal LLM in image-service, model open; Q10 rename and delete only; Q11 analyzer runs Docling; Q12 write-back over the catalog API via evilflowers-protocol's generated Python client with an API key; Q13 `:main` in development, release tags in production; Q14 search-service run as-is. Updated the IP-015 row, Phase 3, 6 and 7 accordingly |
