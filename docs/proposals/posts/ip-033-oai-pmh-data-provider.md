---
draft: true
date: 2026-11-16
authors:
  - jdubec
categories:
  - Interoperability
  - API
tags:
  - oai-pmh
  - dublin-core
  - marc21
  - metadata
  - harvesting
  - dataverse
---

# IP-033: OAI-PMH 2.0 Data Provider

Make the catalog's publications harvestable over OAI-PMH 2.0, so the STU library's discovery system and
open aggregators can list Elvira's books next to the rest of the university collection, with a link back
to the portal. A new app `apps/oai` serves `/oai` from its own table of harvestable records. The table is
kept in step with the catalog by a sync that moves a record's datestamp only when the record a harvester
would see actually changes. Formats: `oai_dc` (mandatory) and `marc21` (MARCXML).

<!-- more -->

## Status

✅ **Accepted** (2026-11-16). All Review Questions are answered (see the Resolutions); implementation is
tracked in #196. The decisions that change the text below:

| Topic | Decision |
|---|---|
| Exposure (Q1) | `Catalog.harvestable`, default off. An entry is exposed when its catalog is harvestable, it has an acquisition, its LCP encryption (if any) is `registered`, its origin source (if any) allows re-exposure (Q7), and it is not withdrawn (IP-034). LCP titles carry an access statement |
| Identifiers (Q2) | `oai:evilflowers.elvira.stuba.sk:<uuid>`; base URL `https://evilflowers.elvira.stuba.sk/oai` (settings) |
| Sets (Q3) | catalog `url_name` + `<url_name>:<category id>`; `ListSets` is paged (IP-034 creates categories for every harvested subject) |
| Formats (Q4) | `oai_dc` and `marc21` now |
| Read noise (Q5) | sync tasks deduplicated per entry |
| Link-back (Q6) | omitted when `content.cite_base_url` is empty |
| Re-exposure (Q7) | entries imported from a source flagged `oai_reexpose = false` (a source that is an OAI provider itself, e.g. Dataverse) are not exposed; entries mirrored *to* such a system are. No system is special-cased (IP-034 unification) |
| External ids (Q8, Q11) | hstore keys in `Entry.identifiers`, one per external system, configured in `EVILFLOWERS_EXTERNAL_SYSTEMS`; no new link model; the Dataverse keys stay as they are |
| `035` code (Q9) | `EVILFLOWERS_MARC_ORG_CODE`, `ELVIRA` until STU agrees a code with CVT |
| Resolver (Q10) | `/resolve/isbn/{isbn}` and `/availability?isbn=`, anonymous, harvestable catalogs only |

## Problem Statement

Students and staff search the university library's catalogue or discovery layer, not Elvira. A book that
exists only in Elvira is invisible there. Discovery systems and aggregators ingest external collections
almost universally through OAI-PMH. The catalog speaks OPDS (for reading apps), a REST API and MCP, none
of which a library system harvests.

Three properties of the current data model stand in the way of a correct OAI-PMH endpoint:

1. **No usable datestamp.** OAI-PMH harvests incrementally ("records changed since `from`"). A datestamp
   must change when the record's metadata changes (spec §2.7.1). `Entry.updated_at` and `touched_at`
   change on every save, and opening a book saves the entry
   (`apps/files/tests/test_versioned_covers.py:113`). An incremental harvest built on them would re-send
   every book that was read since the last harvest. Conversely, author renames, category renames,
   `QuerySet.update()` calls and catalog changes alter what a harvester would see without saving the
   entry at all.
2. **Deletes leave no trace.** Entries are hard-deleted (cascading from `Catalog`). A harvester can learn
   about a deletion only from a deleted-record header (§2.5.1). Without one, a deleted book stays in the
   university catalogue until someone runs a full re-harvest.
3. **No exposure rule.** Nothing says which catalogs are meant to be harvested, and whether the metadata of
   a restricted (LCP, loan-only) title is public.

## Proposed Solution

![Components](../../diagrams/component-oai-pmh.svg)

### One table of harvestable records

`apps.oai.models.OaiRecord` (`oai_records`) holds one row per entry that has ever been harvestable:

| Field | Type | Meaning |
|---|---|---|
| `id` | bigint | tiebreaker for paging |
| `entry_id` | UUID, unique, **no foreign key** | the entry; the row outlives it |
| `set_specs` | `text[]` (GIN) | sets the record belongs to, frozen into tombstones |
| `datestamp` | timestamptz, whole seconds, UTC | the OAI datestamp; index `(datestamp, id)` |
| `deleted` | bool | tombstone |
| `fingerprint` | char(64) | SHA-256 of the canonical projection + crosswalk version |
| `projection` | JSONB, null when deleted | everything the crosswalks render, already resolved |

![Data model](../../diagrams/class-oai-record.svg)

Harvests read only this table. Crosswalks render XML from `projection`, so the served record is exactly
the one that was fingerprinted and datestamped, and a `ListRecords` page needs one indexed query and no
joins.

### The fingerprint decides the datestamp

`OaiRecordSync.sync(entry_ids)` loads each entry, decides whether it is exposed, builds its
`RecordProjection` and setSpecs, and compares the fingerprint with the stored one:

- equal: no write (a read, a popularity bump, an unrelated field);
- different, or no row yet: write projection, setSpecs, fingerprint and
  `datestamp = date_trunc('second', now())`;
- entry gone or no longer exposed: `deleted = true`, `projection = null`, `datestamp = now`.

Triggers:

- **Signals, on commit.** `post_save`/`post_delete` on `Entry`, `EntryAuthor`, `Acquisition`;
  `m2m_changed` on `Entry.categories`; `post_save` on `Author`, `Category`, `Catalog` (affected entry ids).
  The receiver enqueues `oai.sync(entry_ids)` on the default Celery queue after the transaction commits.
- **Nightly reconcile** (beat): every exposed entry in batches, plus every live row whose entry no longer
  exists or is no longer exposed. It catches `QuerySet.update()`, bulk operations and crosswalk upgrades
  (a new crosswalk `version` changes every fingerprint once, which is what §2.7.1 asks for when a format
  changes).

The write is a single autocommit statement issued after the edit's transaction committed, so a harvester
that used the previous `responseDate` as its next `from` cannot skip it (except for a sub-second window,
see Trade-offs).

![Sync](../../diagrams/sequence-oai-record-sync.svg)

### Record lifecycle

![States](../../diagrams/state-oai-record.svg)

`Identify` declares `deletedRecord=persistent`: tombstones are kept forever. They are small (one row per
entry ever exposed; the catalog holds ~1000 entries).

### The protocol endpoint

`OaiPmhView` at `/oai` (GET and POST, `application/x-www-form-urlencoded`, anonymous, CSRF-exempt,
response `text/xml; charset=utf-8`). The gateway's `location /` already proxies every path to the catalog
(`knowledge ansible/roles/nginx/files/sites/evilflowers.elvira.stuba.sk`), so no edge change is needed.
Base URL: `{EVILFLOWERS_BASE_URL}/oai`.

- `RequestParser` enforces the verb grammar (§4): required, optional and exclusive arguments; a
  repeated key, an unknown argument, `from > until`, mixed granularities or an unparseable date →
  `badArgument`; missing, unknown or repeated verb → `badVerb`. On these two errors the `<request>`
  element carries no attributes (§3.2).
- `OaiRepository` implements the six verbs. Errors are OAI XML with HTTP 200, never the API's JSON
  problem documents.
- Granularity `YYYY-MM-DDThh:mm:ssZ`; day-granularity `from`/`until` are accepted (§3.3.1: `from` = start
  of day, `until` = end of day).
- `responseDate` is taken once at the start of the request (guidelines §3); an absent `until` becomes
  `responseDate`.
- Paging is a keyset walk on `(datestamp, id)`, 100 records per page (setting).
- **Resumption tokens are stateless**: the original arguments, the frozen `until`, the last
  `(datestamp, id)`, `cursor` and `completeListSize`, signed with `django.core.signing` (salt `oai`).
  Any worker serves the next page; re-issuing a token returns the same page (§3.5.1). A bad signature →
  `badResumptionToken`. No `expirationDate` is sent (tokens do not expire).
- Empty list → `noRecordsMatch`; unknown `metadataPrefix` → `cannotDisseminateFormat`; unknown identifier
  → `idDoesNotExist`.

![Harvest](../../diagrams/sequence-oai-pmh-harvest.svg)

### Identifiers and sets

- Identifier: `oai:<repository-identifier>:<entry uuid>` (guidelines-oai-identifier §2). The repository
  identifier is a new setting `EVILFLOWERS_OAI_REPOSITORY_IDENTIFIER` (prod `evilflowers.elvira.stuba.sk`,
  Q2), **not** `INSTANCE_NAME`, whose default `evilflowers.local` is not a domain. `Identify` carries the
  `oai-identifier` description container.
- Sets: one per exposed catalog (`setSpec` = `url_name`, `setName` = title) and, nested under it, one per
  category in use (`<url_name>:<category id>`, `setName` = label and code). Ids, not terms, because
  setSpecs allow only URI-unreserved characters and must not change when a librarian fixes a label.

### Crosswalks

`oai_dc` (namespace `http://www.openarchives.org/OAI/2.0/oai_dc/`, schema `oai_dc.xsd`, elements
`http://purl.org/dc/elements/1.1/`, all repeatable):

| Element | From |
|---|---|
| `dc:title` | `title` |
| `dc:creator` | authors in `position` order, `Surname, Name` |
| `dc:subject` | each category: the code with `scheme` (e.g. `UDC 621.3`) and its label |
| `dc:description` | `summary` |
| `dc:publisher` | `publisher` |
| `dc:date` | `published_at` (partial ISO date) |
| `dc:type` | `Text` (DCMI type) |
| `dc:format` | acquisition MIME types |
| `dc:identifier` | portal URL (`content.citation.entry_url`), `urn:isbn:…`, `https://doi.org/…` |
| `dc:language` | `Language.alpha2` |
| `dc:rights` | licence from `entry.rights`; an access statement for restricted titles |

`marc21` (namespace `http://www.loc.gov/MARC21/slim`, schema
`http://www.loc.gov/standards/marcxml/schema/MARC21slim.xsd`; prefix per guidelines-marcxml):

| Field | From |
|---|---|
| Leader | `nam`, electronic resource defaults |
| `001` / `005` | entry id / datestamp |
| `008` | date entered, publication year, `Language.alpha3` |
| `020 $a` | ISBN |
| `024 7_ $a … $2 doi` | DOI |
| `041 $a` | `Language.alpha3` |
| `080 $a` | `term` of categories whose `scheme` is `http://udcdata.info/` (the MDT codes) |
| `100 1_` / `700 1_` | first author / further authors |
| `245 10 $a` | title |
| `264 _1 $b $c` | publisher, year |
| `300 $a` | `page_count` pages |
| `506` | access restriction for LCP/loan-only titles |
| `520 $a` | summary |
| `540` | licence |
| `653` | category labels |
| `856 40 $u` | portal URL |

### Addendum: external record ids and catalogue linking (#199)

Library catalogues match harvested records by identifiers and keep their own record numbers. A partner
library asked for exactly that: OAI-based matching that carries the catalogue's record id, and links from
the catalogue to Elvira. STU's ARL (Cosmotron, `https://kis.cvt.stuba.sk/arl-stu/`, base `stu_us_cat`)
already holds many of the same books and already shows e-version links (`856`), for example record
`stu_us_cat*0076487`. **Elvira never writes into ARL and nobody maps records by hand.**

#### External ids (Q8: hstore keys)

"This entry is record R in system S" is a key in `Entry.identifiers`: `identifiers["arl_stu"] = "0076487"`.
The systems are configuration, `EVILFLOWERS_EXTERNAL_SYSTEMS` (JSON in the environment):

```json
[{"key": "arl_stu", "name": "STU library (ARL)", "kind": "ils", "marc_org_code": "ELVIRA-ARL",
  "record_url_template": "https://kis.cvt.stuba.sk/arl-stu/sk/detail-stu_us_cat-{record_id}/"}]
```

- The allowed keys of `identifiers` become `EVILFLOWERS_IDENTIFIERS` (now also `issn`, `ccnb`) plus every
  configured system key. A new partner is one entry in the setting, no code.
- Every current reader picks known keys (`isbn`, `doi`): OPDS (`apps/opds/services/identifiers.py`),
  citations and the graph ignore the new keys. The REST detail serializer and MCP `get_entry` show the whole
  dict, which is how the portal gets the ARL number.
- Uniqueness of `(key, value)` cannot be a migration-time index for configured keys; the matcher (IP-034)
  enforces it under a per-source advisory lock. Provenance (matched on what, when) is kept in IP-034's
  `HarvestRecord`, not on the entry.
- `Entry.origin` (new, `elvira` · `dataverse` · `harvest:<source>`) records where an entry came from; Q7
  excludes `origin = dataverse`.

The projection gains the external ids, so a new link changes the fingerprint and the record's datestamp.
A harvester sees its own number come back on the next incremental harvest.

- `marc21`: `035 $a (<org code>)<record_id>` per external id, plus Elvira's own
  `035 $a (<EVILFLOWERS_MARC_ORG_CODE>)<entry uuid>` (Q9); `856 40 $u` portal URL with
  `$y Požičať v Elvíre`.
- `oai_dc`: external permalinks as `dc:relation`.

Links are set by IP-034's harvesting client (matching) or by hand (REST/MCP `update_entry` identifiers).

#### ARL: read-only, automatic

1. **Matcher.** An IP-034 harvest source `arl-stu` in `link_only` mode reads ARL's OAI-PMH (`marc21`,
   incremental `from=`) if CVT enables it for `stu`. Otherwise it reads ARL's REST API (`record`/`list`
   endpoints) with a read-only token, one ISBN at a time. It matches only on an exact normalised identifier
   (`020`, `022`, `015`, `024`) with **exactly one** hit and stores an `EntryExternalId`. Zero or several
   hits leave the entry unlinked and are listed in the run report; nothing is fuzzy, nothing waits for a
   person. The link is `identifiers["arl_stu"]`. ARL reports deletions only transiently
   (`deletedRecord=transient` on other ARL installations), so links are re-verified monthly and dropped
   when the record is gone.
2. **Elvira as an e-book provider (ARL's choice).** ARL already takes e-loan provider feeds (Palmknihy,
   eReading) and puts "borrow online" on the *existing* bibliographic record. This proposal's `marc21` with
   `020`, `035` and `856` is such a feed. Whether and how ARL attaches it is up to CVT and Cosmotron.
3. **Portal.** An entry with an ARL link shows "Also in print at STU" with the permalink (portal issue, once
   links exist).
4. **Optional resolver** (Q10): `GET /resolve/isbn/{isbn}` → the portal entry, and
   `GET /availability?isbn=` → `{copies, available, next_free}`, so an IPAC template could show live
   status without touching a record.

**Questions for CVT STU / Cosmotron** (asked, not probed):

1. Is OAI-PMH (`/i2/i2.ws.oai.cls`) enabled for `stu`? Which sets (monographs, e-resources, authorities)?
2. Can Elvira get a read-only REST/ws-API token, and which index codes does STU use for ISBN, ISSN, ČNB?
3. How does ARL ingest e-loan provider feeds: does it match existing records by ISBN or create new ones?
   Would it accept Elvira as a provider?
4. Would STU allow an IPAC widget that calls Elvira's resolver/availability by ISBN?
5. What feeds the FCHPT VuFind (`digilibs.fchpt.stuba.sk`) that holds STU records (ids `stu.<T001>`)?

## Implementation Plan

### Phase 1 — Record table and sync

- [ ] `apps/oai` app: `OaiRecord` model + migration (indexes `(datestamp, id)`, GIN `set_specs`,
      unique `entry_id`)
- [ ] Exposure rule (per Q1), incl. the `Catalog` field and its migration if chosen
- [ ] `Projector` (`Entry` → `RecordProjection`, setSpecs), canonical JSON + fingerprint
- [ ] `OaiRecordSync.sync(entry_ids)` and `reconcile()`; whole-second datestamp in an autocommit statement
- [ ] Signal receivers (on commit) and the Celery task `oai.sync`, deduplicated per entry (Q5); triggers
      include acquisition and LCP encryption changes, because exposure depends on them (Q1)
- [ ] Beat entry `oai.reconcile` (nightly; schedule setting, empty disables)
- [ ] Management command `oai_sync [--all | --entry <uuid>] [--dry-run]` for the initial backfill
- [ ] Settings: `EVILFLOWERS_OAI_ENABLED`, `EVILFLOWERS_OAI_REPOSITORY_IDENTIFIER`,
      `EVILFLOWERS_OAI_REPOSITORY_NAME`, `EVILFLOWERS_OAI_PAGE_SIZE`; admin e-mail from
      `EVILFLOWERS_CONTACT_EMAIL`
- [ ] Tests: a read does not move the datestamp; a title edit, author rename, category add, file add do;
      delete, catalog withdrawal and catalog cascade leave tombstones; re-exposure revives the row

### Phase 2 — Protocol and `oai_dc`

- [ ] `RequestParser` (GET + POST) with the full argument grammar and error mapping
- [ ] `OaiRepository`: Identify (with `oai-identifier` description), ListMetadataFormats, ListSets,
      GetRecord, ListIdentifiers, ListRecords
- [ ] `ResumptionTokenCodec` (signed, stateless keyset)
- [ ] `OaiDc` crosswalk
- [ ] `OaiPmhView` at `/oai` (anonymous, CSRF-exempt, `text/xml`)
- [ ] Tests: every verb, every error code of §3.6, paging across pages with a change mid-walk, token
      tampering, day vs seconds granularity, deleted records in lists and GetRecord; responses validated
      against `OAI-PMH.xsd` and `oai_dc.xsd` (schemas vendored for offline tests)

### Phase 3 — `marc21`

- [ ] `Marc21` crosswalk (table above), validated against `MARC21slim.xsd`
- [ ] Mapping reviewed with the STU library (Q4)

### Phase 4 — Rollout

- [ ] Docs: `docs/oai/` (base URL, sets, formats, how datestamps work)
- [ ] knowledge issue: env vars for dev and prod, backfill command, reconcile schedule
- [ ] Harvest the dev endpoint with an independent harvester or validator before announcing the
      prod base URL
- [ ] Hand the base URL to the STU library; register with BASE if Q1 makes the collection public

### Phase 5 — External ids and ARL linking (addendum, #199)

- [ ] `EVILFLOWERS_EXTERNAL_SYSTEMS` registry; allowed `identifiers` keys = bibliographic keys + system
      keys; `Entry.origin` + data migration (`dataverse` where `dataverse_pid` is set)
- [ ] Projection external ids; `035` and `856 $y` in `marc21`, `dc:relation` in `oai_dc`
- [ ] Send the CVT/Cosmotron questions; register `arl-stu` once OAI or a REST token is available
- [ ] `arl-stu` harvest source in `link_only` mode (runs on IP-034's harvesting client); monthly re-verify
- [ ] Portal issue: "Also in print at STU"
- [ ] Resolver/availability endpoints, if Q10 says yes

## Technical Details

- **Why not compute datestamps from `updated_at`.** It changes on reads (above) and does not change on
  author/category renames. A fingerprint over the rendered projection changes exactly when the record
  changes, set membership included, by construction.
- **Why no foreign key from `OaiRecord` to `Entry`.** A tombstone must survive `Entry` deletion and the
  `Catalog` cascade. The sync detects deletions on commit (`post_delete` fires per entry, also in a
  cascade, because receivers exist) and in the nightly reconcile (rows whose entry is gone).
- **Ordering and paging.** `ORDER BY datestamp, id` with a keyset `WHERE (datestamp, id) > (:ds, :id)`
  is stable under inserts; a record changed mid-walk moves past the frozen `until` and comes with the
  next harvest (§5: either outcome is allowed).
- **Counting.** `completeListSize` is an exact `COUNT` on the first page (cheap at this size) and carried
  in the token.
- **Link-back URL.** `content.citation.entry_url` (portal entry dialog). When `content.cite_base_url` is
  empty, the record carries no portal URL (Q6); setting it later changes every fingerprint once.
- **Tracing.** The view and the sync get spans in the IP-029 vocabulary (`oai.verb`, `oai.prefix`,
  `oai.set`, records returned).

## Alternatives Considered

### Datestamp column on `Entry`

A `metadata_modified_at` column bumped by signals. Rejected: it cannot see changes that do not save the
entry (renames, `update()`), and it gives tombstones nowhere to live.

### Periodic MARC file export instead of a live endpoint

Many libraries load MARC files. Cheaper, but needs a person to run every load, and it carries no deletions.
The `Marc21` crosswalk can produce such a file later from the same table if the library prefers it.

### Off-the-shelf providers (e.g. `django-oai-pmh`, `pyoai` server)

The protocol surface is small. The hard part here is the record table, datestamps and tombstones, which no
library provides for this model; a dependency would add its own model on top.

### OAI-PMH as the Dataverse ingest transport

The current Dataverse integration is a push: a Dataverse pre-publish workflow calls
`POST /api/v1/dataverse-prepublish`, and `DataverseSyncService` pulls the dataset metadata and file list
over the native API. The Dataverse software ships an OAI-PMH data provider (`/oai`), so the catalog could
instead *harvest* it (IP-008 lists this as future work). Whether our instance has it enabled, and whether
any of its formats carries file ids and the `restricted` flag that `_sync_acquisitions` needs, is
unverified: the dev instance answered 502 on 2026-11-16. That is the opposite role of this proposal
(harvester, not provider). It would replace the workflow, its token and the resume callback with a
periodic pull that also recovers missed events and upstream deletions. It would lose the pre-publish
timing (records appear after publication, on the harvest interval). It is not part of this proposal; see
Q7.

## Trade-offs and Risks

| Risk | Mitigation |
|---|---|
| A sync commits its datestamp a moment after a harvester's query started with the same second | Datestamp written in its own autocommit statement after the edit committed; residual window under a second. Harvesters conventionally overlap `from` by the granularity |
| Every reader opening a book enqueues a sync task | Receiver skips saves whose `update_fields` touch only non-projected columns; tasks deduplicated per entry (Q5) |
| Restricted titles' metadata becomes public | Exposure is opt-in per catalog (Q1); `dc:rights`/`506` state the restriction |
| Category ids in setSpecs are unreadable | `setName` carries the label and code; ids keep sets stable across renames |
| Identifier namespace differs between dev and prod | Explicit setting; dev uses its own domain so dev records never collide with prod |
| Crosswalk changes re-send the whole collection | Intended (§2.7.1); ~1000 records |

## Success Criteria

- [ ] Every response validates against `OAI-PMH.xsd` and its metadata schema
- [ ] Opening a book does not change its datestamp; editing its title, authors, categories or files does
- [ ] Deleting an entry, its catalog, or withdrawing the catalog yields a deleted header in the next
      incremental harvest
- [ ] A full harvest of the dev endpoint by an independent harvester completes with no errors
- [ ] The STU library's discovery system lists Elvira titles with working portal links

## Future Considerations

- OAI-PMH harvester for Dataverse (Q7).
- `oai_openaire` or `datacite` formats if open-access content is added and OpenAIRE becomes a target.
- Sitemaps and schema.org metadata on portal pages (Google, Google Scholar), from the same projection.
- The `Marc21` crosswalk as a batch export.

## References

- OAI-PMH 2.0 specification: <http://www.openarchives.org/OAI/openarchivesprotocol.html>
- Implementation guidelines (repositories): <http://www.openarchives.org/OAI/2.0/guidelines-repository.htm>
- OAI identifier format: <http://www.openarchives.org/OAI/2.0/guidelines-oai-identifier.htm>
- MARCXML in OAI-PMH: <http://www.openarchives.org/OAI/2.0/guidelines-marcxml.htm>
- Diagrams: `docs/diagrams/{component-oai-pmh,sequence-oai-pmh-harvest,sequence-oai-record-sync,state-oai-record,class-oai-record}.puml`
- IP-008 (Dataverse consolidation, "OAI-PMH ingest" future item), IP-029 (tracing vocabulary)

## Discussion

The proposal came from a question about Z39.50 and OAI-PMH (2026-11-16). Z39.50 was dropped: no harvester
or discovery system needs a Z39.50 server from an e-book platform; as a client (copy cataloguing by ISBN)
it may return as a separate idea.

## Review Questions

**Status**: ✅ Resolved (2026-11-16)
**Review Date**: 2026-11-16
**Reviewer**: Claude AI

The following questions must be answered before implementation:

---

### Q1: Which catalogs are harvestable, and is restricted metadata public? 🔴 Critical

**Issue**: The class diagram adds `Catalog.harvestable`; nothing in the catalog decides exposure today.
`Catalog.is_public` controls reading access, not cataloguing visibility.

**Context**: Harvested metadata ends up in the university catalogue and possibly in public aggregators.
LCP/loan-only titles are most of the STU collection.

Entry-level cases fall under the same rule: an entry with no acquisition yet (metadata created before the
upload, as in the bulk import), or an LCP title whose encryption is not `registered`, would be harvested
with a portal link that cannot be read or borrowed yet.

**Question**: How is exposure decided, are restricted titles exposed (metadata only, with an access
statement), and are entries without a readable file held back?

**Options**:
- [x] **A**: New `Catalog.harvestable` flag, default off, set per catalog; restricted titles included with
      `dc:rights`/`506` access statements; entries exposed only once they have an acquisition and, for LCP,
      a `registered` encryption (recommended)
- [ ] **B**: Every `is_public` catalog is harvestable; restricted titles excluded
- [ ] **C**: A settings-store list of catalog `url_name`s; no model change

**Answer**:
```
A (jdubec, 2026-11-16)
```

**Resolution**:
```
Catalog.harvestable (bool, default False). Exposed = harvestable catalog AND an acquisition exists AND
(not readium_enabled OR encryption registered) AND origin source allows re-exposure (Q7) AND not withdrawn
(IP-034 Q6).
Acquisition and EncryptedContent changes are sync triggers. LCP titles: dc:rights / 506 access statement.
```

---

### Q2: Repository identifier and base URL ⚠️ Medium

**Issue**: OAI identifiers must use a domain the repository controls. The API host is
`evilflowers.elvira.stuba.sk`, the portal `elvira.stuba.sk`.

**Question**: Which domain names the repository, and which base URL is published?

**Options**:
- [ ] **A**: Identifier `oai:elvira.stuba.sk:<uuid>`, base URL `https://evilflowers.elvira.stuba.sk/oai`;
      dev `oai:dev.elvira.stuba.sk:<uuid>` (recommended)
- [x] **B**: Identifier and base URL both on `evilflowers.elvira.stuba.sk`

**Answer**:
```
B (jdubec, 2026-11-16)
```

**Resolution**:
```
EVILFLOWERS_OAI_REPOSITORY_IDENTIFIER = evilflowers.elvira.stuba.sk (dev: dev.evilflowers.elvira.stuba.sk);
base URL = {EVILFLOWERS_BASE_URL}/oai.
```

---

### Q3: Set design ⚠️ Medium

**Issue**: Category sets with ids are stable but unreadable in a harvester's configuration. The `stu` terms
are UDC codes (`scheme` `http://udcdata.info/`, e.g. `004`, `621.3`; checked 2026-11-16). They are readable,
but UDC syntax uses `:`, `/` and `+`, and a setSpec element must not contain `:` (§2.6); a corrected code
would also move the set.

**Question**: Which sets are offered?

**Options**:
- [x] **A**: Catalog sets plus nested category sets keyed by category id (recommended)
- [ ] **B**: Catalog sets only
- [ ] **C**: Catalog sets plus category sets keyed by the MDT code

**Answer**:
```
A (jdubec, 2026-11-16)
```

**Resolution**:
```
setSpecs "<url_name>" and "<url_name>:<category uuid>"; setName = label (and "UDC <term>" for UDC).
IP-034 Q3 creates a category for every harvested subject, so ListSets uses resumption tokens.
```

---

### Q4: What does the STU library's system want? ⚠️ Medium

**Issue**: The format choice (`marc21` depth, fixed fields, `856` link conventions) depends on the
consuming system, which is not known yet.

**Question**: Which discovery/library system will harvest, which format and sets does it expect, and who
reviews the MARC mapping?

**Options**:
- [ ] **A**: Ship `oai_dc` first (Phase 2), build `marc21` after the library names its system and reviews
      the mapping (recommended)
- [x] **B**: Ship both formats as specified above and adjust after the first harvest

**Answer**:
```
B (jdubec, 2026-11-16)
```

**Resolution**:
```
oai_dc and marc21 in the first release; the marc21 mapping is revisited when CVT answers.
```

---

### Q5: Avoid a sync task per read ⚠️ Medium

**Issue**: Opening a book through a user acquisition runs a plain `entry.save()` with no `update_fields`
(`apps/files/views.py:212`, popularity + 1), so the `Entry` `post_save` receiver cannot tell a read from an
edit and would enqueue a task per read (Sequence "a reader opens the book"). The `Acquisition` receiver
(`apps/core/models/acquisition.py:138`) saves the entry again on every acquisition save.

**Question**: How is the noise cut?

**Options**:
- [x] **A**: Deduplicate per entry: the receiver enqueues `oai.sync` only if no sync for that entry is
      pending (a short-lived cache key), and the task compares fingerprints (cheap) (recommended)
- [ ] **B**: As A, and change `views.py:212` to the atomic `update(popularity=F(...) + 1)` used at line 152.
      This changes existing behaviour: the read would no longer bump `touched_at`, which
      `test_versioned_covers` asserts and OPDS feed ETags use
- [ ] **C**: No signals; reconcile every few minutes instead (datestamps lag by that much)

**Answer**:
```
A (jdubec, 2026-11-16)
```

**Resolution**:
```
Receivers enqueue oai.sync(entry_id) only when the cache key oai:sync:<entry_id> is absent (set for 60 s);
the task compares fingerprints. The read path is unchanged.
```

---

### Q6: Link-back URL when `content.cite_base_url` is empty ℹ️ Low

**Issue**: `entry_url()` returns a relative path when the setting store value is empty.

**Question**: Fall back to `EVILFLOWERS_PORTAL_URL`, then `EVILFLOWERS_BASE_URL`, or omit the link?

**Options**:
- [ ] **A**: Fall back as listed (recommended)
- [x] **B**: Omit `dc:identifier`/`856` URLs until the setting is set

**Answer**:
```
B (jdubec, 2026-11-16)
```

**Resolution**:
```
No portal URL in dc:identifier / 856 while content.cite_base_url is empty. Setting it changes every
fingerprint once, so harvesters receive the links on the next incremental harvest.
```

---

### Q7: Dataverse — expose its entries, and harvest it? ⚠️ Medium

**Issue**: Entries imported from Dataverse (`identifiers.dataverse_pid`) describe datasets that Dataverse
can publish over its own OAI-PMH provider (enabled on our instance: unverified). If it does, exposing them
again would duplicate records in aggregators. Separately,
OAI-PMH could replace the pre-publish workflow as the ingest transport (Alternatives).

**Question**: Are Dataverse-imported entries harvestable from Elvira, and should an OAI harvester for
Dataverse become its own proposal?

**Options**:
- [x] **A**: Exclude entries with a `dataverse_pid` from this provider; open a separate proposal to
      evaluate a Dataverse OAI harvester against the workflow (recommended)
- [ ] **B**: Include them (with `dc:relation` to the DOI); no harvester proposal
- [ ] **C**: Include them and plan the harvester

**Answer**:
```
A (jdubec, 2026-11-16); the harvester is IP-034
```

**Resolution**:
```
Excluded by origin, not by the presence of dataverse_pid, and per source rather than per system (user,
2026-11-16): Entry.origin = "harvest:<slug>"; a HarvestSource with oai_reexpose = false (Dataverse, other
OAI providers) keeps its imports out. Today's Dataverse imports become origin "harvest:dataverse" (data
migration). Elvira books mirrored to Dataverse keep origin "elvira" and stay harvestable.
```

---

### Q8: Where do external record ids live? ⚠️ Medium

**Issue**: The addendum adds `EntryExternalId`. Entries already keep `identifiers` (hstore with a fixed key
list in `EVILFLOWERS_IDENTIFIERS`) for ISBN, DOI and the Dataverse ids.

**Question**: New model, or new hstore keys per system?

**Options**:
- [ ] **A**: New `ExternalSystem` + `EntryExternalId` models; `identifiers` keeps bibliographic ids (ISBN,
      DOI) only (recommended: one row per link with provenance, a unique `(system, record_id)`, no code
      change per new partner)
- [x] **B**: Add hstore keys (`arl_stu`, …) per system

**Answer**:
```
B (jdubec, 2026-11-16)
```

**Resolution**:
```
Keys per system in Entry.identifiers, systems configured in EVILFLOWERS_EXTERNAL_SYSTEMS (key, name, kind,
marc_org_code, record_url_template). Allowed keys = EVILFLOWERS_IDENTIFIERS (+ issn, ccnb) + system keys.
Uniqueness enforced by IP-034's matcher; provenance kept in HarvestRecord. No ExternalSystem/EntryExternalId
models.
```

---

### Q9: Elvira's MARC organization code ℹ️ Low

**Issue**: `035 $a (<code>)<id>` needs a code for Elvira. MARC organization codes are registered with the
Library of Congress; Slovak libraries also use national sigla.

**Question**: Which code prefixes Elvira's own `035`?

**Options**:
- [x] **A**: STU library's existing MARC code or siglum with a suffix agreed with CVT (recommended)
- [ ] **B**: A free-text prefix `(ELVIRA)` until a code is registered

**Answer**:
```
A (jdubec, 2026-11-16)
```

**Resolution**:
```
EVILFLOWERS_MARC_ORG_CODE, default "ELVIRA" until STU agrees the code with CVT (question 1 to CVT).
```

---

### Q10: Public resolver and availability endpoints? ⚠️ Medium

**Issue**: `GET /resolve/isbn/{isbn}` and `GET /availability?isbn=` let a catalogue show live loan status
without record changes, but they reveal which ISBNs Elvira holds and how many copies are on loan.

**Question**: Build them, and are they anonymous?

**Options**:
- [x] **A**: Build both; anonymous for entries in harvestable catalogs (Q1), rate-limited (IP-026)
      (recommended)
- [ ] **B**: Resolver only
- [ ] **C**: Neither until a catalogue asks

**Answer**:
```
A (jdubec, 2026-11-16)
```

**Resolution**:
```
GET /resolve/isbn/{isbn} (302 to the portal entry, 404 otherwise) and GET /availability?isbn= (copies,
available now, next free date), anonymous, only for exposed entries. They sit behind SecuredView's rate
limits, which are off by default (IP-026), so "rate-limited" holds only once those are enabled.
```

---

### Q11: Move the Dataverse ids into `EntryExternalId`? ℹ️ Low

**Issue**: `identifiers.dataverse_pid` has a partial unique constraint (`core/0038`) and is used by the
Dataverse sync; IP-034 links Dataverse datasets through `EntryExternalId`.

**Question**: Migrate them, or keep both?

**Options**:
- [ ] **A**: Data migration into `EntryExternalId` (system `dataverse`), keep the hstore keys read-only for
      one release, then drop (recommended)
- [x] **B**: Keep `dataverse_pid` in `identifiers`; Dataverse stays a special case

**Answer**:
```
Moot after Q8 = B (jdubec, 2026-11-16)
```

**Resolution**:
```
The Dataverse keys stay in identifiers with their partial unique index (core/0038).
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-11-16 | Claude AI | Initial draft from the OAI-PMH 2.0 spec and guidelines; five diagrams under `docs/diagrams/` |
| 2026-11-16 | Claude AI | Added Review Questions section (Q1–Q7) |
| 2026-11-16 | Claude AI | Review pass: Q5 options rewritten (a read saves without `update_fields`), Q1 covers entries without a readable file, Q3 notes UDC syntax vs setSpec, `080` keyed on the udcdata.info scheme, Dataverse OAI support marked unverified, diagrams aligned on rename signals |
| 2026-11-16 | Claude AI | Addendum (#199): `ExternalSystem`/`EntryExternalId`, `035`/`856`/`dc:relation` output, read-only automatic ARL linking (matcher on IP-034, provider-feed option, resolver), questions for CVT/Cosmotron, Phase 5, Review Questions Q8–Q11 |
| 2026-11-16 | Claude AI | Review Questions answered by jdubec, resolutions written; status Accepted. Q2 identifiers on `evilflowers.elvira.stuba.sk`, Q4 both formats now, Q6 link omitted when unset, Q8 hstore keys + `EVILFLOWERS_EXTERNAL_SYSTEMS` (link models dropped), Q7 exclusion by `Entry.origin`; exposure also requires an acquisition and registered encryption |
| 2026-11-16 | Claude AI | Q7 generalised with IP-034's unification: re-exposure is a per-source flag (`HarvestSource.oai_reexpose`), origins are `harvest:<slug>` |
| 2026-11-16 | Claude AI | Implemented (uncommitted): `apps/oai` with 77 tests validated against the OAI-PMH, oai_dc and MARC21slim XSDs, plus a round trip with IP-034's harvester; deploy knowledge#73, portal elvira-portal#54 |
