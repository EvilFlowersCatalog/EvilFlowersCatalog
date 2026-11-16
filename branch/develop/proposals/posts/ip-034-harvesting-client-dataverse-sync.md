---
draft: true
date: 2026-11-16
authors:
  - jdubec
categories:
  - Interoperability
  - Integration
tags:
  - oai-pmh
  - marc21
  - dataverse
  - harvesting
  - import
  - sync
  - arl
---

# IP-034: Metadata Harvesting Client and Two-Way Sync (Connectors)

One harvesting client imports, links and synchronises records with other systems through **connectors**:
OAI-PMH (`marc21`, `oai_dc`), MARC files and Dataverse are three implementations of one contract. It
matches records to existing entries by exact identifiers only, then creates, enriches, just links, or keeps
them in sync both ways. The same machinery covers three needs:

- a partner library's import of ~20,000 records;
- the read-only ARL matcher from IP-033's addendum;
- a two-way sync, of which Dataverse is the first connector able to write.

Nothing in the harvesting app knows Dataverse. A connector that can write back (Dataverse: a `book`
dataset type with an `elvira_book` block) lets a source run in `sync` mode, where edits on either side win
per field (last write wins), with an audit trail.

<!-- more -->

## Status

✅ **Accepted** (2026-11-16). All Review Questions are answered. Implementation is in progress and
uncommitted. The Dataverse write path is built against recorded API responses only: the dev Dataverse is
being redeployed (knowledge issue) and has not been reachable since 2026-11-16, so it is **untested live**.
Tracking: #200 (proposal), #196 (IP-033).

## Problem Statement

1. **Metadata is typed by hand.** A partner library testing dev wrote that it would not want to type
   20,000 records, and that most libraries already publish their metadata over OAI-PMH. Elvira has no
   import from external sources. `load_catalog` only restores Elvira's own dumps. The STU bulk import ran
   through a spreadsheet and the MCP `create_entry` / upload-link tools, which suits hundreds of books, not
   tens of thousands.
2. **Catalogue links need matching.** IP-033 stores "entry = record R in system S" as an `identifiers`
   key. Something has to find R automatically, read-only, for ARL and for any partner's catalogue.
3. **Dataverse sync is one-way and thin.**
   - Today a Dataverse publish workflow calls `POST /api/v1/dataverse-prepublish`.
   - `DataverseSyncService` then reads the dataset's `:draft` version over the native API and upserts an
     entry.
   - It maps title, description, authors, publisher/producer, dates, PID, language and licence. It does not
     map ISBN, subjects or UDC codes, series or page count.
   - Its upsert overwrites fields unconditionally and replaces the author list.
   - Nothing is written back, and deletions, deaccessions and moves are never noticed.
   - The user wants entries manageable from Dataverse, as much metadata there as possible, and edits on
     either side to count.

## Decisions (2026-11-16, jdubec)

| Topic | Decision |
|---|---|
| Matching (Q1) | exact identifiers only |
| Equal titles (Q2) | imports refuse only on equal ISBN/DOI or the same source record; editions go to a new `Entry.edition` column. Import-only: the librarian path keeps its rule |
| Subjects (Q3) | a category is created for every new subject (scheme kept) |
| Dataverse versions (Q4) | published versions only |
| Outbound (Q5) | superuser token (environment variable, never the settings store) + `publish?type=updatecurrent` |
| Deaccession (Q6) | the entry is **withdrawn**: hidden from every reader-facing list and search, not borrowable, running loans end normally; IP-033 turns it into a deleted record |
| Mirroring (Q7) | the dev Dataverse is intranet-only during the pilot, so mirrored datasets are published normally |
| Files (Q8) | never uploaded to Dataverse; the dataset links to Elvira (`elviraUrl`, `relatedMaterial`) |
| Validation (Q9) | a pre-publish check refuses `book` datasets with an invalid ISBN or no UDC code |
| Conflicts (Q10) | per-field last write wins on record-level timestamps, audited |
| Abstraction (user, 2026-11-16) | one connector contract and registry in `apps/harvest`, in the manner of IP-031's `JobDefinition`; Dataverse, OAI-PMH, MARC files (and later ARL) are connectors. `apps/harvest` imports nothing from `apps/dataverse` and names no system. Two-way sync is a fourth source mode, `sync` |
| Scope of the generalisation (user) | the new layer only: `identifiers.dataverse_pid` / `dataverse_dataset_id` and their unique index stay (the Dataverse connector's `system_key`); IP-033's "not re-exposed over OAI" becomes a per-source flag instead of `origin = dataverse` |
| Naming (user) | `apps/harvest`, `HarvestSource`, `HarvestRecord` stay |

## Proposed Solution

![Harvest and sync data model](../../diagrams/class-harvest-sync.svg)

### New columns in `core`

| Model | Field | Meaning |
|---|---|---|
| `Entry` | `origin` (str, default `elvira`) | `elvira` · `harvest:<source slug>`; the data migration marks today's Dataverse imports `harvest:dataverse`, the origin of the Dataverse source (slug `dataverse`) |
| `Entry` | `edition` (str, null) | edition statement (MARC `250`, `elvira_book.edition`) |
| `Entry` | `withdrawn_at` (timestamptz, null) | Q6: set → hidden and not borrowable |
| `Catalog` | `harvestable` (bool) | IP-033 Q1 |

Mirroring a catalog to another system is not a catalog column: it is a source in `sync` mode whose
`target_catalog` is that catalog and whose connector config names the remote collection.

`Entry.identifiers` accepts `EVILFLOWERS_IDENTIFIERS` (+ `issn`, `ccnb`) plus the keys of
`EVILFLOWERS_EXTERNAL_SYSTEMS` (IP-033 Q8).

### Part A: the harvesting client (`apps/harvest`)

#### Model

| Model | Holds |
|---|---|
| `HarvestSource` | `slug`, `name`, `adapter` (a **connector** name from the registry, not a `choices` list), `config` (JSON, connector-specific: OAI `base_url`/`metadata_prefix`/`set_spec`, Dataverse collection alias; **never secrets** — tokens stay in the environment), `system_key` (the `identifiers` key its record ids go to, e.g. `arl_stu`, `dataverse_pid`), `target_catalog`, `mode` (`link_only` · `enrich` · `create` · `sync`), `field_policy` (JSON, per field `fill_empty` · `overwrite` · `ignore`; default `fill_empty`), `oai_reexpose` (bool, default true; false for sources that are OAI providers themselves, IP-033 Q7), `schedule` (cron, empty = manual), `enabled`, `cursor` |
| `HarvestRecord` | source, `record_id`, `datestamp`, `deleted`, `raw_hash`, normalised `SourceRecord` (JSONB), `entry` (nullable FK, `SET_NULL`), `status` (`matched` · `created` · `enriched` · `ambiguous` · `unmatched` · `skipped` · `failed` · `deleted`), `matched_on`, `message`, `updated_at`; unique `(source, record_id)`. For `sync` sources also: `synced_record` (JSONB, the record both sides agreed on at the last sync — the merge base), `remote_version`, `local_version`, `sync_state` (`in_sync` · `pending` · `conflict` · `error`) |
| `HarvestConflict` | source, record, entry, `field`, `local_value`, `remote_value`, `local_at`, `remote_at`, `winner`, `acknowledged` (only `sync` sources) |

Runs are IP-031 jobs (`harvest.run`), so there is no run model: counters and the summary are the run's
report.

#### Connectors → one `SourceRecord`

A connector is a class registered by name (`apps/harvest/adapters/registry.py`), in the manner of IP-031's
`JobDefinition` registry: the app that knows a system implements the contract in its own `connector.py`
and registers it; `apps/harvest` never imports it.

| Capability | Method | Who implements it |
|---|---|---|
| read (required) | `records(source, cursor, full)` → `SourceRecord`s + next cursor; `fetch(source, record_id)` → `SourceRecord` or deleted; `test(source)` | every connector |
| write (optional) | `push(source, record_id or None, record, expected_version)` → `(record_id, new_version)`; refuses a stale version | Dataverse |
| validate (optional) | `validate(source, record)` → list of problems | Dataverse (pre-publish hook) |
| link (optional) | `record_url(source, record_id)` | every connector that has landing pages |

A source may use mode `sync` only if its connector can write.

- **Fields of `SourceRecord`:** title, subtitle, authors (`surname`, `name`), identifiers (ISBN-13 normalised
  from ISBN-10, ISSN, ČNB, DOI), language (ISO 639-2/B or 639-1 → `Language`), publisher, date, edition,
  series, page count, summary, subjects (`scheme`, `term`, `label`; UDC from MARC `080`), licence, links
  (`856`), the source's own record id, and its version (remote last-update time).
- **OAI-PMH connector:**
  - `Identify` first (granularity, `deletedRecord`);
  - then incremental `ListRecords` with `from = cursor`, resumption tokens and deleted headers;
  - parses `marc21`, `oai_marcxml` and `oai_dc`; MARC through `pymarc` (new dependency).
- **MARC file connector:** ISO 2709 or MARCXML uploaded through the admin API, for partners without OAI.
- **Dataverse connector** (`apps/dataverse/connector.py`): the native API, both directions (Part B).

#### Matching (exact only, Q1)

For each `SourceRecord`, in order:

1. An entry in the target catalog with `identifiers[system_key] == record_id` → that entry.
2. Otherwise, normalised ISBN, ISSN, ČNB, DOI against entries in the target catalog:
   - **exactly one** entry → match, and `identifiers[system_key] = record_id` is set;
   - **none** → `unmatched`;
   - **more than one** → `ambiguous`.
3. A `system_key` value already held by *another* entry → `ambiguous` (uniqueness the database cannot
   index).

Runs of one source hold a PostgreSQL advisory lock, so two runs cannot assign the same id.

#### Applying, by mode

| Mode | Matched | Unmatched | Deleted at source |
|---|---|---|---|
| `link_only` (ARL) | `system_key` set, entry otherwise untouched | nothing | key removed |
| `enrich` | fields applied per `field_policy` | nothing | key removed |
| `create` (partner import) | as `enrich` | new entry, `origin = harvest:<slug>` | key removed; the entry stays (a librarian decides) |
| `sync` (Dataverse) | three-way merge per field (Part B) | new entry, `origin = harvest:<slug>`; entries of the target catalog without a record are pushed (created remotely) | entry withdrawn (Q6) |

- **Writes:** through `EntryForm` + `EntryService.populate`, with an `import_mode` flag that applies Q2's
  rule (equal titles allowed when identifiers differ). Validation, search indexing and IP-033's OAI sync
  behave as for a librarian's edit.
- **Authors:** reused per catalog by name (`Author.objects.get_or_create`), in record order.
- **Subjects:** `Category` by `(catalog, scheme, term)`, created when missing (Q3). UDC keeps
  `http://udcdata.info/`; other MARC subject fields use the source's `slug` as scheme.

#### Files after metadata

- **Command:** `harvest_attach_files --source <slug> --dir <path> --match isbn|record_id|filename
  [--dry-run]` attaches files through `attach_acquisition`.
- **`856` URLs** to files on the source are stored as external-URL acquisitions only when the source's
  `field_policy` allows `links`.

#### Runs

- **Job kind:** `harvest.run`, subject `HarvestSource`, spec `{dry_run: bool, full: bool}`.
- **Started from:** a beat entry per source schedule, `POST /api/v1/jobs`, the MCP `run_harvest` tool or the
  management command `harvest <slug> [--dry-run] [--full]`.
- **Dry run:** goes through matching and reports what would happen. It writes neither entries nor the
  source cursor; `HarvestRecord`s are written with a `dry_run` marker, so the portal can page through them.

### Part B: two-way sync (generic), and the Dataverse connector

The sync is part of `apps/harvest` and works with any connector that can write. Dataverse is the first
one. Everything Dataverse-specific is the connector (client, mapper both ways, its two webhook views,
the metadata block); merge, conflicts, push, the reconcile, the endpoints and the resolver are generic.

![Two-way sync](../../diagrams/sequence-dataverse-sync.svg)

#### Dataverse facts

From the 6.x guides (workflows, native API, metadata customisation, external tools, read 2026-11-16):

- Workflows exist only around publishing: `PrePublishDataset`, `PostPublishDataset`.
- There is no webhook for deletes, deaccessions, file changes, draft edits or moves.
- `editMetadata` supports optimistic locking via `sourceLastUpdateTime`.
- `publish?type=updatecurrent` (superuser) updates the current version without a new version number.
- Dataset types (6.4+) can be linked to metadata blocks.
- Dataset-level external tools get signed URLs.

#### Metadata model in Dataverse

- **Dataset type:** `book`, linked to the `citation` and `elvira_book` blocks, allowed in the Elvira
  collection(s).
- **Custom block `elvira_book`:** a TSV in this repository (`docs/dataverse/elvira_book.tsv`).
  - Fields: `isbn` (multiple), `edition`, `seriesTitle`, `seriesNumber`, `pageCount`, `udc` (multiple),
    `elviraEntryId`, `elviraUrl`, `lending`.
  - `elviraEntryId`, `elviraUrl` and `lending` are written by Elvira only.
  - `udc` is free text validated by the pre-publish check: the vocabulary grows with Q3, so it is not a
    TSV controlled vocabulary.

Citation block mapping, used in both directions:

| Elvira | Dataverse |
|---|---|
| `title` | `title` |
| authors (position order) | `author.authorName` ("Surname, Name") |
| `summary` | `dsDescription.dsDescriptionValue` |
| `publisher` | `distributor.distributorName` (inbound fallback `producerName`) |
| `published_at` | `distributionDate` (inbound fallback `productionDate`) |
| `language` | `language` |
| non-UDC categories | `keyword.keywordValue` |
| `rights` licence | dataset licence |
| ISBN, DOI | also `otherId` (agency `ISBN` / `DOI`) |
| portal URL | `relatedMaterial` (Q8) |

#### Inbound (remote → Elvira)

- **Generic:** `apply_record(source, record_id)` fetches the record through the connector and runs it through
  the same match/apply pipeline as a harvest run; in `sync` mode that is the three-way merge.
- **Reconcile:** a `sync` source's own `schedule` (e.g. every 15 minutes) runs `harvest.run`: the connector's
  `records(cursor)` returns what changed since the cursor and reports deleted records, so changes and
  deletions no webhook reports are found. A deleted record withdraws its entry (Q6).
- **Dataverse webhooks** (thin views in `apps/dataverse`, URLs kept as knowledge#73 names them):
  - `POST /api/v1/dataverse-prepublish` (`PostPublishDataset` `http/sr`) → `apply_record` on the source whose
    connector is Dataverse; the connector reads `:latest-published` (Q4), never `:draft`.
  - `POST /api/v1/dataverse-validate` (`PrePublishDataset` `http/sr`, `expectedResponse` `OK.*`) → the
    connector's `validate`: for type `book`, valid ISBN checksums and ≥ 1 `udc` value, else
    `FAILED: <reason>`, which stops the publish (whether Dataverse shows the reason is unverified; it is also
    logged).
- The Dataverse connector's `records` uses the search API (`type=dataset`, `sort=date`) and
  `versions?includeDeaccessioned=true`; deaccessioned or 404 → deleted.

#### Outbound (Elvira → remote)

- **Trigger (generic):** entry changes (on-commit signals, per-entry dedupe as in IP-033) enqueue
  `harvest.push` (IP-031 job, subject `Entry`, spec `{source}`) for every enabled `sync` source whose target
  catalog holds the entry.
- **Push (generic):** fetch the remote record, three-way merge, then `connector.push(record_id, merged,
  expected_version)`; a stale version → re-fetch, re-merge, retry once. An entry without a record is
  created remotely (`record_id = None`); the returned id goes to `identifiers[system_key]`, `origin` stays
  `elvira`.
- **Dataverse connector `push`:** `PUT …/editMetadata?replace=true` with `sourceLastUpdateTime`, then
  `publish?type=updatecurrent`; creation is `POST /api/dataverses/{config.collection}/datasets` with
  `datasetType=book`, then publish; it also stores `dataverse_dataset_id`. The superuser token comes from
  `EVILFLOWERS_DATAVERSE_SUPERUSER_TOKEN`; without it the connector reports itself read-only. No files (Q8).

#### Conflicts: per-field last write wins (Q10)

There is no separate sync-state model: the merge base is `HarvestRecord.synced_record`, the `SourceRecord`
both sides agreed on at the last sync, with `remote_version` and `local_version` (`Entry.updated_at`) of that
moment. The local side is the entry projected to a `SourceRecord` by the same code that applies records.

On every merge, per field (a pure function, tested with a fake in-memory connector):

- **Unchanged on both sides:** nothing.
- **Changed on one side:** that side's value goes to the other.
- **Changed on both sides:** the side whose record changed later wins. The losing value is written to
  `HarvestConflict` (entry, source, field, both values, both timestamps, winner, acknowledged).

**Loop guard:** after a push, `synced_record` and both versions take the pushed state. The remote's
callback (Dataverse's publish workflow) then finds no difference, and nothing is written.

#### Withdrawn entries (Q6)

`Entry.withdrawn_at` follows the precedent of `Region.objects.visible()`: an explicit queryset method
`Entry.objects.listed()` applied at reader-facing surfaces, **not** a filtered default manager, because loan,
return and file paths look entries up by id and must keep working for running loans.

- **Surfaces:** REST entry list and search, OPDS 1.2 and 2.0 feeds and search, MCP search, the assistant's
  search, graph/similar, IP-033.
- **Gates:** new loans and reservations are refused with 409.
- Admins see withdrawn entries with `?withdrawn=true`.

#### Links in both directions

- `GET /resolve/<source slug>?id=<record id>` (generic) → 302 to the portal entry whose
  `identifiers[system_key]` is that id, or 404. For Dataverse it backs the dataset-level external tool "Open in
  Elvira" (`/resolve/dataverse?id={datasetPid}`, manifest in `docs/dataverse/external-tools/`).
- The other way: `GET /api/v1/entries/{id}/sync` returns, per linked source, the remote record URL
  (`connector.record_url`), so the portal shows "View in Dataverse", "Also in print at STU" and the like
  without knowing any system.

### Admin API contract (for the portal)

| Purpose | Endpoint |
|---|---|
| Sources | `GET/POST /api/v1/harvest/sources`, `GET/PUT/DELETE /api/v1/harvest/sources/{id}` |
| Test a source | `POST /api/v1/harvest/sources/{id}/test` → Identify, formats, sets, a sample record (no writes) |
| MARC upload | `POST /api/v1/harvest/sources/{id}/upload` (multipart `file`) for `marc_file` sources |
| Start a run | `POST /api/v1/jobs` `{"kind": "harvest.run", "subject_id": "<source id>", "spec": {"dry_run": true, "full": false}}` (IP-031) |
| Runs and progress | `GET /api/v1/jobs?kind=harvest.run&subject_id=<source id>`; the run's `report` holds counters |
| Records of a source | `GET /api/v1/harvest/sources/{id}/records?status=&dry_run=&query=&page=&limit=` |
| Connectors | `GET /api/v1/harvest/connectors` → name, title, capabilities (`read`, `write`, `validate`, `link`), config schema — the portal builds the source form from it |
| External systems | `GET /api/v1/external-systems` (key, name, URL template; from settings) |
| Entry sync state | `GET /api/v1/entries/{id}/sync` → per linked source: source, record id, record URL, mode, last sync, state (`in_sync` · `pending` · `conflict` · `error`), last error, open conflicts |
| Pull / push now | `POST /api/v1/jobs` kind `harvest.pull` / `harvest.push` with the entry id and `{"source": "<source id>"}` |
| Conflicts | `GET /api/v1/harvest/conflicts?source_id=&entry_id=&acknowledged=`, `POST /api/v1/harvest/conflicts/{id}/acknowledge` |
| Resolver | `GET /resolve/<source slug>?id=<record id>` → 302 to the portal entry |
| Catalog settings | `harvestable` on the existing catalog endpoints (mirroring = a `sync` source with this target catalog) |
| Withdraw / restore | `withdrawn` (bool) on the existing entry update; `?withdrawn=true` on the entry list |
| OAI status | `GET /api/v1/oai/status` → base URL, records live/deleted, earliest datestamp, last sync (IP-033) |

All `/api/v1/harvest/*` endpoints need `manage` on the target catalog, or superuser.

## Implementation Plan

### Phase 1: core columns and settings

- [ ] `Entry.origin`, `Entry.edition`, `Entry.withdrawn_at`, `Catalog.harvestable` + migration (data
      migration for `origin`)
- [ ] `EVILFLOWERS_EXTERNAL_SYSTEMS`, identifier keys `issn`, `ccnb`, `EVILFLOWERS_MARC_ORG_CODE`,
      `EVILFLOWERS_DATAVERSE_SUPERUSER_TOKEN`
- [ ] Serializers and forms expose the new fields

### Phase 2: harvesting core and adapters

- [ ] `apps/harvest`: `HarvestSource`, `HarvestRecord`, `SourceRecord`, identifier normalisation, matcher,
      applier, `import_mode` in `EntryService`
- [ ] `OaiPmhAdapter`, `MarcFileAdapter` (`pymarc`)
- [ ] `harvest.run` job kind, admin API, MCP tools, commands `harvest`, `harvest_attach_files`
- [ ] Tests, including a full round trip that harvests Elvira's own IP-033 provider

### Phase 3: ARL link-only source

- [ ] `arl_stu` system and source once CVT answers; monthly re-verify (a scheduled `full` run)

### Phase 4: generic two-way sync

- [ ] Connector capabilities (`write`, `validate`, `link`) in the registry; mode `sync` allowed only for
      writing connectors; `oai_reexpose` on sources (IP-033 Q7)
- [ ] `HarvestRecord` sync fields, `HarvestConflict`; three-way merge (pure); `apply_record`; `harvest.push` /
      `harvest.pull` jobs; outbound signals; remote creation of unlinked entries
- [ ] Endpoints: connectors, entry sync state, conflicts, generic resolver
- [ ] Tests against a fake in-memory connector: merge rules, loop guard, conflicts, withdrawal on remote
      deletion; `apps/harvest` contains no system name (grep)

### Phase 5: Dataverse connector

- [ ] Client (latest-published, search, versions, editMetadata, updatecurrent, create), mapper both ways,
      `connector.py` registering the connector; webhooks as thin views calling `apply_record` / `validate`
- [ ] `docs/dataverse/elvira_book.tsv`, external tool manifest, operator steps (knowledge#73)
- [ ] Tests for client, mapper and webhooks only

### Phase 6: withdrawn entries and portal

- [ ] `Entry.objects.listed()` on every reader-facing surface, one test per surface; borrow/reserve gates
- [ ] Portal issues (parent + sub-issues)

## Technical Details

- **Idempotency:** `HarvestRecord.raw_hash` makes unchanged records free.
- **Scale:** 20,000 records in pages of the source's size; entry writes in batches of 100 per transaction;
  search indexing and embeddings stay asynchronous.
- **Dataverse access:** inbound uses the existing API token; outbound `updatecurrent` and dataset creation
  use the superuser token from the environment. The workflow resume endpoint is IP-whitelisted, so the
  catalog's egress IP must be on `/api/admin/workflows/ip-whitelist`.
- **Pilot safety:** sources are disabled until enabled; mirroring only for catalogs with
  `dataverse_collection`. Without Dataverse, Elvira works unchanged.

## Alternatives Considered

### Dataverse's own OAI-PMH instead of the native API

Dataverse refreshes OAI sets nightly, reports deletions transiently, and it is undocumented which formats
carry file ids. The native API has all of it. OAI stays the adapter for every *other* source.

### SWORD v2 for writing into Dataverse

Dataverse supports only about a dozen DC terms over SWORD, and a replace replaces all metadata.

### COAR Notify (Announce Relationship)

An inbound announcement only notifies a curator. Both systems are ours, so direct API writes are simpler.

### Field ownership instead of last write wins

Simpler and recommended by the research, but the user chose editing in both places. It is the fallback if
conflicts prove frequent.

### A filtered default manager for withdrawn entries

Rejected: loan, return and file paths look entries up through the default manager and must keep working
for running loans.

## Trade-offs and Risks

| Risk | Mitigation |
|---|---|
| A wrong automatic match | Exact identifiers only, single hit; dry run first; `link_only` for catalogues we must not affect |
| Last write wins loses an edit | `SyncConflict` audit, per-entry conflict view, acknowledge flow |
| Superuser token in the catalog | Environment variable, used only by the outbound task; never in the settings store (readable via MCP `get_setting`) |
| Create-all subjects floods categories and OAI sets | `ListSets` paged; categories scoped per catalog |
| Unique external ids are app-enforced | Advisory lock per source; ambiguous on collision |
| Dataverse write path untested live | Off by default; recorded-response tests; first live run on dev after redeploy |

## Success Criteria

- [ ] A partner's OAI endpoint can be dry-run and then imported into a test catalog without typing a record;
      a re-run changes nothing
- [ ] ARL links appear for exactly-matching ISBNs and nothing is written to ARL
- [ ] A librarian edits title, ISBN, UDC and series in Dataverse and sees them in Elvira after publishing
- [ ] An edit in Elvira appears in the dataset without a new version number
- [ ] Conflicting edits are resolved per field and listed
- [ ] A deaccessioned dataset's entry disappears from listings while its loans keep working

## References

- Dataverse guides (6.x): workflows, native API, metadata customisation, external tools, harvesting
  (https://guides.dataverse.org/en/latest/), read 2026-11-16
- OAI-PMH 2.0, MARCXML guidelines (openarchives.org); MARC 21 `020`, `035`, `080`, `250`, `856`
- ARL OAI and REST: Cosmotron product pages; LIBCAS/ASEP-ARL `arl-rest-api` OpenAPI files (2026-11-16)
- IP-008 (Dataverse consolidation), IP-031 (jobs), IP-033 (OAI-PMH provider and addendum)
- Issues #196, #199, #200

## Discussion

Started from a partner library's e-mail (2026-11-16): import via OAI-PMH, OAI with the catalogue's record
id, links from the catalogue to Elvira, OpenID login (out of scope for now).

## Review Questions

**Status**: ✅ Resolved (2026-11-16)
**Review Date**: 2026-11-16
**Reviewer**: Claude AI

---

### Q1: Exact-identifier matching only? ⚠️ Medium

**Options**:
- [x] **A**: Exact identifiers only (recommended)
- [ ] **B**: Also title + first author + year, reported as candidates only

**Answer**:
```
A (jdubec, 2026-11-16)
```

**Resolution**:
```
Matcher: system_key, then ISBN/ISSN/ČNB/DOI, single hit; otherwise unmatched or ambiguous.
```

---

### Q2: Duplicate-title rule during imports 🔴 Critical

**Options**:
- [x] **A**: Imports refuse only on equal ISBN/DOI or equal source record; equal titles allowed when
      identifiers differ; edition stored separately (recommended)
- [ ] **B**: Append "(edition/year)" to the title
- [ ] **C**: Skip such records

**Answer**:
```
A (jdubec, 2026-11-16)
```

**Resolution**:
```
EntryService.populate(..., import_mode=True) skips the title check when the record's ISBN/DOI differ from
the clashing entry's. New column Entry.edition. The librarian path (REST/MCP) keeps the title rule.
```

---

### Q3: Subjects that don't exist in the catalog ⚠️ Medium

**Options**:
- [ ] **A**: Create only UDC categories
- [x] **B**: Create a category for every new subject
- [ ] **C**: Never create

**Answer**:
```
B (jdubec, 2026-11-16)
```

**Resolution**:
```
Category by (catalog, scheme, term), created when missing. UDC keeps http://udcdata.info/, other subject
fields use the source slug as scheme. IP-033 ListSets is paged because of the larger number of sets.
```

---

### Q4: Which Dataverse versions reach Elvira? ⚠️ Medium

**Options**:
- [x] **A**: Published only (recommended)
- [ ] **B**: Drafts too

**Answer**:
```
A (jdubec, 2026-11-16)
```

**Resolution**:
```
Inbound reads :latest-published explicitly; the workflow trigger is PostPublishDataset.
```

---

### Q5: Superuser token and `updatecurrent` 🔴 Critical

**Options**:
- [x] **A**: Superuser token, `updatecurrent` (recommended)
- [ ] **B**: Minor versions
- [ ] **C**: Leave Elvira edits as drafts

**Answer**:
```
A (jdubec, 2026-11-16)
```

**Resolution**:
```
EVILFLOWERS_DATAVERSE_SUPERUSER_TOKEN (environment, like the existing DATAVERSE_API_TOKEN); not the settings
store, which MCP get_setting can read. Used only by dataverse.push.
```

---

### Q6: Deaccessioned or deleted datasets 🔴 Critical

**Options**:
- [x] **A**: Hide the entry, keep loans running (recommended)
- [ ] **B**: Delete immediately
- [ ] **C**: Only drop the link

**Answer**:
```
A (jdubec, 2026-11-16)
```

**Resolution**:
```
Entry.withdrawn_at; Entry.objects.listed() at every reader-facing surface (not a filtered default manager);
new loans/reservations refused; IP-033 exposes it as a deleted record.
```

---

### Q7: Mirror the existing ~432 STU books into Dataverse? ⚠️ Medium

**Answer**:
```
"For now we are working in dev - we will use that catalog - there will be no public access to those
publications." Follow-up: "We will have dev dataverse visible only in intranet." (jdubec, 2026-11-16)
```

**Resolution**:
```
Datasets are published normally; access is restricted at the network level (intranet-only dev Dataverse,
part of the redeploy in the knowledge issue). Mirroring is per catalog via Catalog.dataverse_collection.
```

---

### Q8: Files of open titles in Dataverse ℹ️ Low

**Options**:
- [ ] **A**: Upload once
- [x] **B**: Never upload; link to Elvira

**Answer**:
```
B (jdubec, 2026-11-16)
```

**Resolution**:
```
No file uploads; elviraUrl and relatedMaterial carry the portal URL.
```

---

### Q9: Enforce validation before publish? ℹ️ Low

**Options**:
- [x] **A**: Yes, for dataset type `book` (recommended)
- [ ] **B**: No

**Answer**:
```
A (jdubec, 2026-11-16)
```

**Resolution**:
```
POST /api/v1/dataverse-validate for an http/sr PrePublishDataset step; "OK" or "FAILED: <reason>".
```

---

### Q10: Is dataset-level last write wins precise enough? ⚠️ Medium

**Options**:
- [x] **A**: Accept (recommended)
- [ ] **B**: Field ownership for chosen fields

**Answer**:
```
A (jdubec, 2026-11-16)
```

**Resolution**:
```
SyncState field hashes + record-level timestamps; SyncConflict audit with acknowledge.
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-11-16 | Claude AI | Initial draft from the partner library e-mail, Dataverse 6.x, ARL and sync-standards research; user decisions on last write wins, scope, pilot status and ARL |
| 2026-11-16 | Claude AI | Added Review Questions section (Q1–Q10) |
| 2026-11-16 | Claude AI | Review Questions answered by jdubec; rewritten to the decisions: hstore external ids (no link models), `origin`/`edition`/`withdrawn_at`, import-only title rule, create-all subjects, `:latest-published`, superuser token from the environment, no file uploads, pre-publish check, admin API contract for the portal; status Accepted |
| 2026-11-16 | Claude AI | Unified per the user: one connector contract and registry; two-way sync is a generic `sync` mode in `apps/harvest` (merge base on `HarvestRecord`, `HarvestConflict`, `harvest.push`/`harvest.pull`, generic resolver and endpoints); Dataverse reduced to a connector (client, mapper, webhooks); `Catalog.dataverse_collection` dropped (mirroring is source config); `origin = harvest:<slug>` everywhere; `oai_reexpose` per source replaces the `origin = dataverse` rule |
| 2026-11-16 | Claude AI | Implemented (uncommitted): `apps/harvest` (connectors, matching, modes incl. `sync`, merge, conflicts, jobs, API, MCP, resolver), Dataverse connector, withdrawn entries; full suite green except the pre-existing tracing test; Dataverse calls tested against hand-built responses only; portal issues elvira-portal#51–#55, deploy knowledge#73 |
