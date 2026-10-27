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
**Last Updated**: 2026-10-27
**Implementation**: Not started

This is a **parallel proposal**. It overlaps with [IP-012](ip-012-ai-powered-discovery.md),
[IP-013](ip-013-document-extraction-structure.md) and [IP-015](ip-015-assistant-fold-in.md), but it
does not wait for them and they do not wait for it. Where two proposals describe the same piece,
**whichever is implemented first owns it** and the other adapts to what shipped. See
[Relationship to other proposals](#relationship-to-other-proposals) for each overlap, including the
statements this proposal directly contradicts.

## Relationship to other proposals

### Parallel rule

- A shared piece (the extraction app, its models, the worker, the storage layout) is defined by
  whichever proposal implements it first.
- The later proposal updates its own text to build on what exists instead of re-specifying it, and
  logs that in its Changelog.
- Contradictions listed below are resolved in favour of this proposal **for the viewer use case
  only**. They do not change the goals of the other proposals.

### Overlaps and contradictions

| Proposal | What it says | What IP-018 says | Kind |
|---|---|---|---|
| IP-013 [Scope & non-goals](ip-013-document-extraction-structure.md#scope-non-goals) | "Explicit non-goal: read-time PDF highlight/anchoring … no per-passage highlight quads, no anchor resolver … and no new Viewer highlight render path." | Agent highlights are stored as positioned areas on the page and shown in the viewer. Quotes are resolved to page positions. | **Contradiction** |
| IP-013 [Review Q2](ip-013-document-extraction-structure.md#q2-read-time-anchoring-deferred-text-based-stored-bboxes-or-hybrid) | Answered "do NOT build monad-knowledge-style highlighting". | Builds exactly that, limited to agent highlight layers. | **Contradiction** |
| IP-013 [Scope & non-goals](ip-013-document-extraction-structure.md#scope-non-goals) | "Feature bounding boxes are kept only for cropping/display of figures/tables." | Feature positions are a first-class output: the viewer uses them to locate each figure, table and equation on the reader's page. | **Contradiction** (narrow) |
| IP-013 [Proposed Solution](ip-013-document-extraction-structure.md#proposed-solution) | `apps/extraction` runs on the IP-012 `discovery` worker image. | `apps/extraction` runs in an extra container from the **unchanged catalog image**, on its own `extraction` queue. | Overlap (where it runs) |
| IP-013 [Data Model Changes](ip-013-document-extraction-structure.md#data-model-changes) | Separate `extraction_figure`, `extraction_table`, `extraction_equation` tables. | One `extraction_region` table with a `kind`. Whichever lands first decides. | Overlap (schema) |
| IP-013 [Phase 2](ip-013-document-extraction-structure.md#phase-2-feature-extraction-docling-figures-tables-math-images) and [Phase 3](ip-013-document-extraction-structure.md#phase-3-classification-captioning) | Docling feature extraction, then VLM captions and classification. | Same tools and steps, but triggered on upload for the viewer. Sections, passages and subject classification stay with IP-013. | Overlap (shared work) |
| IP-012 [Phase 1](ip-012-ai-powered-discovery.md#phase-1-event-driven-indexing-pipeline-replaces-61) | Retire `text-service` and `search-service`. | Agrees. Neither runs in production today (see [Production state](#production-state-2026-10-27)). | Agreement, with new evidence |
| IP-015 [D1](ip-015-assistant-fold-in.md#d1-tool-set-exactly-three-read-only) | The chat assistant has exactly three read-only tools. | Adds region tools for the viewer context, and an agent that **writes** an annotation. | **Contradiction** (tool set) |
| IP-015 [D12](ip-015-assistant-fold-in.md#d12-relationship-to-ip-012) | The assistant has no retrieval work. | Unchanged. IP-018 tools read one publication the reader has open, which is not catalog-wide retrieval. | Compatible |

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
  are in the development `compose.yml` only.
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
- No production feature depends on any of the service repositories, so retiring them breaks
  nothing.
- PostgreSQL and Redis are not containers on this host. Where they run is not yet recorded here.

Production storage is confirmed as `FileSystemStorage`, mounted from
`/opt/elvira-devops/environments/production/storage` to `/var/local/evilflowers`.

## Proposed Solution

### Overview

1. **Extract on upload.** When a PDF acquisition is stored, the catalog queues an extraction task.
   A worker finds every figure, table and equation, records its page and position, crops it to an
   image, and describes it: a caption and category for figures, markdown for tables, LaTeX for
   equations.
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
- One extraction worker container in production.

**Out of scope**

- Any user interface. Viewer behaviour is prototyped separately and not specified here.
- Sections, passages, page labels and subject classification
  ([IP-013](ip-013-document-extraction-structure.md) Phases 1 and 3).
- Chunking, embeddings, search, recommendations ([IP-012](ip-012-ai-powered-discovery.md)).
- EPUB and other formats. PDF only.
- Adding text and positions to existing reader annotations. They keep only their SVG.

### How it runs

The extraction code lives in the catalog repository as `apps/extraction`. Production runs it as
**one additional container from the same catalog image**, with a different command:

```yaml
evilflowers-extraction:
  image: <the evilflowers image>
  command: celery -A evil_flowers_catalog.celery worker -Q extraction --concurrency=1
  volumes:
    - /opt/elvira-devops/environments/production/storage:/var/local/evilflowers
    - extraction-models:/models
```

- The `evilflowers` container's own Celery worker does not listen on `extraction`, so heavy work
  never runs next to gunicorn.
- Docling and torch become dependencies of the catalog image. The pipeline modules are imported
  only inside the extraction task, so the web process never loads them.
- Model weights are kept in a volume, not in the image.
- The worker writes to the same database through the same Django models. There is no HTTP hop and
  no second copy of the schema.
- The extraction container runs as the same user as `evilflowers`, so files it writes can be
  deleted by the catalog.
- Flower, already in production, shows the `extraction` queue and the backfill progress.

## Implementation Plan

### Phase 1: Extraction app and storage

- [ ] Create `apps/extraction` with `ExtractionRun` and `Region` models and migrations
      (see [Data Model Changes](#data-model-changes)).
- [ ] Storage helper for `catalogs/<catalog>/<entry>/extraction/<acquisition_id>/v<N>/`, written
      through `apps.files.storage.get_storage()`.
- [ ] Remove the acquisition's `extraction/` folder in `AcquisitionStorageService.delete`.
- [ ] Add an `extraction` Poetry dependency group and install it in the catalog image.
- [ ] Route `extraction.*` tasks to the `extraction` queue in the Celery configuration.

### Phase 2: Pipeline

- [ ] `process_acquisition(acquisition_id)` task with task id
      `extract:<acquisition_id>:v<pipeline_version>`, so duplicate sends are dropped.
- [ ] Source step: open the original file from storage with PyMuPDF, record page count and
      page sizes.
- [ ] Docling pass: layout, figures (`generate_picture_images`), tables (`do_table_structure`),
      formulas (`do_formula_enrichment`). `do_ocr` only when the page has no text layer.
- [ ] Figures: crop, store as WebP, apply size and aspect gates (reject under 80 px, extreme
      aspect ratios, and anything covering over 90% of the page).
- [ ] Tables: markdown, caption if found, crop.
- [ ] Equations: LaTeX, normalized with sympy where it parses; crop.
- [ ] Captions and categories for figures with a local vision-language model, behind a setting so
      it can be switched off when no GPU is available.
- [ ] Write a new `ExtractionRun`, then make it the acquisition's active run in one transaction
      and delete the previous run's files.
- [ ] Failures on one page or one region are logged and skipped. A document never fails as a whole
      because of one bad region.

### Phase 3: Triggers and backfill

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
      reading order, then a short summary built from those quotes.
- [ ] Quote resolution: find each quote on its page with PyMuPDF `search_for(..., quads=True)` on
      the original file, map the page to the reader's numbering, store the areas as page fractions.
      Quotes that cannot be found are dropped and counted in `unresolved_count`.
- [ ] Generate `content` for each agent item: an SVG of `rect` elements, one per quad, in the
      coordinate system agreed in Q7, so the existing PDF export draws it.
- [ ] Endpoint to request an agent annotation from a prompt. Listing, reading, renaming and
      deleting use the existing `annotations` and `annotation-items` endpoints, which return the
      new fields.
- [ ] Agent annotations are read-only apart from the title: updates to their items are rejected.
- [ ] Ownership is unchanged: `AnnotationFilter` already limits users to their own annotations.

### Phase 7: Production

- [ ] Add `evilflowers-extraction` to the production compose.
- [ ] Decide GPU or CPU for the worker (see Review Questions).
- [ ] Run the backfill, watching the queue in Flower.

### Prerequisites

- A PostgreSQL migration window for the new tables.
- A host for the extraction worker that can mount production storage.

## Technical Details

### Technology Stack

- **Docling** for layout, figures, tables and formulas in one pass (as chosen in
  [IP-013 Q1](ip-013-document-extraction-structure.md#q1-born-digital-feature-extractor-docling-unified-or-a-la-carte)).
- **PyMuPDF** for page geometry, cropping and quote resolution.
- **sympy** for LaTeX normalization.
- A local **vision-language model** for figure captions and categories. The same choice as
  IP-013 Phase 3 if that lands first.
- **Celery** on a dedicated `extraction` queue, broker as today.
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
- Only crops are files. Positions, captions, markdown and LaTeX live in PostgreSQL.
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
    ADD COLUMN quads jsonb,                                    -- agent only: [{x0, y0, x1, y1}, ...] page fractions
    ADD COLUMN text  text,                                     -- agent only: the highlighted sentence
    ADD COLUMN note  text;                                     -- agent only
-- page:    unchanged meaning, the reader's page number, as the viewer already stores it
-- content: still SVG; for agent items generated by the server from quads
```

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
EVILFLOWERS_EXTRACTION_ENABLED=1
EVILFLOWERS_EXTRACTION_PIPELINE_VERSION=1
EVILFLOWERS_EXTRACTION_CAPTIONS_ENABLED=1        # off on CPU-only hosts
EVILFLOWERS_EXTRACTION_CAPTION_MODEL=...
HF_HOME=/models
```

## Alternatives Considered

### Alternative 1: Run the existing service repositories as containers

**Pros:** code exists. **Cons:** none of them run in production today, image-service throws away
positions, equation-service never finds regions, all of them speak Kafka, which production does not
run, and results would need a second copy of the schema or a write-back API. **Not chosen.**

### Alternative 2: A separate extraction service with its own image

**Pros:** web image stays smaller. **Cons:** a second build, a second deploy, and the Django models
duplicated or replaced by an HTTP API. **Not chosen.** The same image with a different command gives
the same isolation at runtime.

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

- **Heavier catalog image** in exchange for one codebase, one schema and one build.
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
| Upload queue grows during backfill | Low | Separate queue; rate-limited command; Flower |
| Contradicts IP-013's no-highlighting decision | Medium | Recorded in [Relationship to other proposals](#overlaps-and-contradictions); limited to the viewer |

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
- [ ] Agent annotations appear through the existing annotation endpoints and in PDF downloads with
      `?annotations=true`, with no change to how reader annotations behave.
- [ ] Deleting an acquisition removes its extraction files.
- [ ] `evilflowers-extraction` runs in production from the unchanged catalog image.

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
  `apps/core/modifiers/pdf/annotations.py`, `apps/api/filters/annotations.py`, `conf/supervisor.conf`
- Viewer: `EvilFlowersViewer` `src/components/pdf-viewer-page/pdf-edit/EditCanvas.vue` (`addHighlight`),
  `src/assets/utils/interfaces.ts` (`IEditPackage`)
- Service repositories: `EvilFlowersCatalog/evilflowers-image-service`, `-text-service`,
  `-equation-service`, `-analyzer-service`, `-search-service`, `-video-service`,
  `-text-service-tp2025`
- Docling, PyMuPDF (`Page.search_for`), sympy

## Review Questions

**Status**: ⏳ Awaiting Answers
**Review Date**: 2026-10-27
**Reviewer**: Claude AI

The following questions must be answered before implementation:

---

### Q1: 🔴 Where does the extraction worker run, and does it have a GPU?

**Issue**: [How it runs](#how-it-runs) assumes the worker mounts production storage. Production has
four containers and no confirmed GPU. IP-013 tracks T4 placement as an open item (#65).

**Context**: Without a GPU, Docling layout still works but figure captions and formula recognition
become slow enough to stall the backfill.

**Question**: Where does `evilflowers-extraction` run?

**Options**:
- [ ] **A**: On the production host, CPU only, captions switched off until a GPU is available (recommended)
- [ ] **B**: On the T4 machine, mounting production storage over the network
- [ ] **C**: On the production host after a GPU is added

**Answer**:
```
```

**Resolution**:
```
```

---

### Q2: 🔴 One region table or IP-013's three feature tables?

**Issue**: [Data Model Changes](#data-model-changes) defines `extraction_region` with a `kind`.
IP-013 defines `extraction_figure`, `extraction_table` and `extraction_equation`.

**Context**: The parallel rule says the first implementation wins. Choosing now avoids a later
migration.

**Question**: Which schema should the first implementation use?

**Options**:
- [ ] **A**: One `extraction_region` table, and IP-013 adopts it (recommended)
- [ ] **B**: IP-013's three tables, and IP-018 adapts

**Answer**:
```
```

**Resolution**:
```
```

---

### Q3: ⚠️ Should IP-013 be amended now or after this proposal is accepted?

**Issue**: IP-013 still lists highlighting as a non-goal, and its Q2 answer rejects it. This
proposal contradicts both.

**Context**: Two Draft proposals with opposite statements will confuse anyone implementing either.

**Question**: How should IP-013 reflect this?

**Options**:
- [ ] **A**: Add a note and Changelog entry to IP-013 pointing here, leave its body unchanged (recommended)
- [ ] **B**: Edit IP-013's non-goal to exclude the viewer use case
- [ ] **C**: Leave IP-013 untouched until one of the two is accepted

**Answer**:
```
```

**Resolution**:
```
```

---

### Q4: ⚠️ Is the highlight agent synchronous or a background task?

**Issue**: [Phase 6](#phase-6-agent-annotations) does not say how long a request may take. The
agent may read many pages before answering.

**Context**: `explain` is synchronous today and bounded by `MAX_TOOL_ROUNDS = 8`. A "teach me X from
this book" request needs far more reading than that.

**Question**: How is a highlight request executed?

**Options**:
- [ ] **A**: Background Celery task on the default queue; the annotation is created with `status = pending` and filled in (recommended)
- [ ] **B**: Synchronous request with a higher tool-round limit
- [ ] **C**: Streamed over SSE like chat messages (IP-015 D5)

**Answer**:
```
```

**Resolution**:
```
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
```

**Resolution**:
```
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

**Answer**:
```
```

**Resolution**:
```
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
- [ ] **A**: PDF points of the page, with a `viewBox` on the SVG root; the viewer scales on render and converts on save (recommended)
- [ ] **B**: Page fractions (0–1) with `viewBox="0 0 1 1"`; the PDF export multiplies by page size
- [ ] **C**: Keep screen pixels and store the zoom alongside; agent SVG is generated at 100%

**Answer**:
```
```

**Resolution**:
```
```

---

### Q8: ℹ️ Which caption model?

**Issue**: IP-013 names Qwen2-VL, and image-service used PaliGemma. This proposal leaves it as a
setting.

**Context**: The assistant already uses an Ollama endpoint, which can serve vision models.

**Question**: Where do captions come from?

**Options**:
- [ ] **A**: The existing Ollama endpoint with a vision model, so no model runs inside the worker (recommended)
- [ ] **B**: Qwen2-VL loaded inside the extraction worker
- [ ] **C**: Docling's built-in picture description

**Answer**:
```
```

**Resolution**:
```
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
- [ ] **A**: One item per highlight; clients accept several items per page for an annotation (recommended)
- [ ] **B**: One item per page, with `quads` and `text` holding a list of highlights and `seq` per highlight inside it

**Answer**:
```
```

**Resolution**:
```
```

---

### Q10: ℹ️ Can a reader edit an agent annotation?

**Issue**: [Phase 6](#phase-6-agent-annotations) makes agent annotations read-only apart from the
title, so the generated SVG never disagrees with `quads` and `text`.

**Context**: A reader may want to draw on top of what the agent marked.

**Question**: What can a reader change?

**Options**:
- [ ] **A**: Only the title; to change it, the reader copies it into a new annotation of kind `highlight` (recommended)
- [ ] **B**: Anything; edits clear `quads` and `text` on the changed item

**Answer**:
```
```

**Resolution**:
```
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-10-27 | kordostomas | Initial draft: extraction on upload, reader-scoped regions, assistant region tools, agent highlight layers; parallel to IP-012/013/015 with overlaps and contradictions listed; logged that no `evilflowers-*-service` container runs in production |
| 2026-10-27 | kordostomas | Added Review Questions section |
| 2026-10-27 | kordostomas | Dropped `highlight_layer`/`highlight`: agent highlights extend `Annotation` and `AnnotationItem` with nullable columns and server-generated SVG in `content`; existing annotation endpoints reused; replaced Q7 (ownership already enforced by `AnnotationFilter`) with the SVG coordinate question; added Q9 (item per highlight or per page) and Q10 (editing agent annotations) |
| 2026-10-27 | kordostomas | Aligned `Annotation.kind` with the viewer's layers: `drawing`, `highlight`, `agent` (third layer), plus `bookmarks` replacing the `__page_bookmarks__` title convention; manual highlights may also fill `text` and `quads` |
