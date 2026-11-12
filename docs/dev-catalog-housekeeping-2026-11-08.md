# elvira-dev catalog housekeeping — 2026-11-08

Target: `https://dev.evilflowers.elvira.stuba.sk` (k8s namespace `evilflowers-dev`), through the `elvira-dev` MCP,
the REST API with the same key, and read-only SQL on `evilflowers_dev`. Production (`elvira` MCP) was only read
(`whoami`, `list_catalogs`), nothing written.

## Starting state

| Catalog | Entries | Feeds | Categories | Notes |
|---|---|---|---|---|
| Open Research Library (`openresearchlibrary`) | 408 | 26 | 3 | Springer OA books + a few IEEE/MDPI papers; FIIT course feed tree |
| STU FCHPT (`fchpt`) | 6 | 0 | 0 | all keyboard-mash titles, Dataverse-imported |
| STU FIIT (`fiit`) | 0 | 1 | 1 | empty feed "Bakalar" |
| Slovak University of Technology | 0 | 0 | 59 | the STU MDT vocabulary, no entries |
| STU FAD, FEI, KU, MTF, SJF, SVF | 0 | 0 | 0 | empty |

LCP: 13 entries had `readium_enabled=true` (12 × 1 copy, 1 × 3 copies), mostly with default viewer config.

## Status of the issues (2026-11-08, end of session)

Code fixes are in the working tree, **uncommitted**, with tests; full suite 664 tests, only the two pre-existing
`apps.dataverse` import errors (`pytest` not installed). None of it is deployed. The orphan fix is feature work that
CLAUDE.md routes through a proposal; it was implemented directly on request — an IP can be backfilled.

| # | Issue | Status | Where |
|---|---|---|---|
| 1 | Deletes leave files in storage | ✅ fixed | `apps/files/signals.py` (post_delete → on_commit removal for Acquisition, Entry, Catalog), `apps/readium/signals.py` (LCP package), `purge_catalog` simplified; tests `apps/files/tests/test_cleanup.py` |
| 1b | Existing orphans / missing files | ✅ tool | `manage.py storage_audit [--catalog] [--delete-orphans] [--format json]`; tests `test_storage_audit.py` |
| 2, 17 | MCP feed results rejected by clients | ✅ fixed | `apps/mcp/server.py` resource-link `name` via `localize()`; test in `test_protocol_surfaces.py` |
| 3 | No MCP `delete_entry` | ✅ added | `apps/mcp/tools/publishing.py` — `dry_run`, refuses live loans/queue unless `force`, reports files & LCP ids; `test_entry_deletion.py` |
| 4 | `whoami` readable < manageable | ✅ fixed | `apps/mcp/tools/library.py` uses `list_catalogs`' `CatalogFilter`; test in `test_tools.py` |
| 5 | Dataverse URLs on `dataverse:8080` | ⏳ config | the integration's base URL must be the public Dataverse host, not the Compose name |
| 6, 15 | Availability ignores encryption state | ✅ fixed (additive) | `entry_lcp_decorator.py`: no free copy until a package is `registered`; new `lcp_ready` (REST) / `package_ready` (MCP). Enum unchanged → portal shows "unavailable". A dedicated `preparing` state needs a portal change |
| 7–10 | url_name, `nan` scheme, IVZ, '' vs NULL | ℹ️ data | IVZ removed; rest documented |
| 12 | Lost encryption callback | ⏳ open | needs reconciliation against `readium-lcp.content` (job registered on LCP, `pending` in Elvira) |
| 13 | Extraction run stuck `running` | ⏳ open | needs a watchdog |
| 14 | 49 book files + covers missing on dev | ⏳ ops | restore from the Compose volume / backup; `storage_audit` lists them |
| 16 | Failed job never retried on toggle | ✅ fixed | `apps/readium/signals.py` re-queues a `failed` job on the off→on switch only; `test_encryption_toggle.py` |
| 18 | `http://` absolute URLs | ✅ fixed elsewhere | commit `43e6500` (public-scheme middleware, Tomáš Kordoš) |
| 19 | MCP cannot set viewer options | ✅ added | `update_entry` / `create_entry`: `ip_block`, `allow_print`, `allow_share`, `allow_annotations` |
| 21 | **New**: an entry that was ever lent cannot be deleted | ✅ fixed | `License.encrypted_content` `PROTECT` → `RESTRICT` (migration `readium/0009`); REST `DELETE` answered 500 `ProtectedError` |
| 22 | **New**: `S3Storage.listdir` broke Django's contract | ✅ fixed | returned minio's iterator; every folder cleanup on S3 silently did nothing |
| 23 | **New**: tests write into the developer's storage dir | ℹ️ open | model fields bind `storage=get_storage` at import, so `override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=…)` does not redirect them (seen: `catalogs/managed/` from the MCP tests). New tests patch the field storage |

Also: #11 (`.mcp.json:88`) is a one-line local fix, not done; #20 only partly — languages fixed (23 × `de`, 1 × `en`),
authors/summaries/capitalisation untouched. REST `DELETE /acquisitions/{id}` with live loans still answers 500
(now `RestrictedError`, before `ProtectedError`) — should be a 409. The `S3Storage.listdir` fix has no test (no minio
in the suite). `delete_entry` / `lcp_ready` are not yet in `docs/catalog-wiki/Model-Context-Protocol.md` (submodule).

⚠️ Migration `readium/0009_license_encrypted_content_restrict` depends on `0008_encryptedcontent_dispatch_tracking`,
which is uncommitted work of a parallel session — re-point it if `0008` is regenerated.

⚠️ Deploy impact of #6: titles whose package is not `registered` with `encrypted_at` set stop showing as available.
On dev that is 0 of 17. Check prod before deploying:

```sql
select count(*) filter (where ec.id is null) no_job,
       count(*) filter (where ec.status <> 'registered') not_registered,
       count(*) filter (where ec.status = 'registered' and ec.encrypted_at is null) registered_no_timestamp
from entries e join acquisitions a on a.entry_id = e.id
left join encrypted_contents ec on ec.acquisition_id = a.id
where e.config->>'readium_enabled' = 'true';
```

Those titles already cannot be lent (`is_ready_for_licensing` applies the same test); a non-zero third column calls
for backfilling `encrypted_at` on registered rows.

### MCP follow-ups (from the friction of this session, not built)

- `search_entries` with `fields: ["id"]` and a higher limit for id-only listings — bulk curation needed full UUID
  lists, fetched via SQL in 4 KB slices.
- `classify_entries` / `add_entries_to_feed` accepting a filter (`category_ids`, a search query) instead of only
  explicit `entry_ids` — "put every MDT 61 title into the Medicine feed" was 51 ids passed by hand.
- `compact: true` on bulk results — every classify response echoed full titles for up to 50 records.

## Issues found

Severity: 🔴 data loss / broken feature · ⚠️ wrong behaviour · ℹ️ cosmetic / docs

### 🔴 1. Deleting an entry, acquisition or catalog leaves its files in storage (orphans)

- `apps/api/views/entries.py:384` OpenAPI description: *"This will also delete all associated acquisition files"*.
  `:390` does `entry.delete()` and nothing else.
- `apps/api/views/acquisitions.py:92` — `acquisition.delete()` only.
- No `post_delete`/`pre_delete` receiver exists for `Entry`, `Acquisition` or `Catalog`; no model overrides `delete()`.
- `AcquisitionStorageService.delete()` (`apps/files/services.py:98`) would remove the payload and the IP-018
  extraction crops, but **has no callers**.
- Files left behind per entry: `Acquisition.content` (the book), `Entry.image`, `Entry.thumbnail`
  (`apps/core/models/entry.py:100,102`), extraction crops; for LCP titles also the encrypted package and the
  publication registered on the LCP server (see LCP orphan check below).
- `delete_catalog` (MCP + REST) cascades every row of the catalog and removes no file at all.

Suggested fix (needs an IP-XXX proposal — feature work):
1. `post_delete` receivers for `Acquisition` (→ `AcquisitionStorageService.delete`) and `Entry` (image, thumbnail),
   wrapped in `transaction.on_commit` so a rolled-back delete keeps its files. Signals, not `delete()` overrides,
   so queryset deletes and catalog cascades are covered.
2. Unregister the publication on the LCP server, or refuse to delete an entry with active loans.
3. `manage.py orphan_files [--delete]` — list storage paths no row references, to clean what is already there.

### ⚠️ 2. MCP `list_feeds` returns malformed content on dev — feeds cannot be listed at all

The client rejects every resource link: `name: expected string, received object`. Feed `title` is jsonb
(`{"sk": "Predmety"}`) and the resource-link builder passes the dict as `name`. Every feed is affected
(content[1..25]), so `list_feeds` is unusable whenever titles are localized.

### ⚠️ 3. MCP has no `delete_entry` tool

Catalogs, feeds and categories can be deleted through the MCP, publications cannot. Clean-up needs the REST API.

### ⚠️ 4. `whoami` counts are incoherent

Superuser on dev: `readable_catalogs: 2`, `manageable_catalogs: 10`, while `list_catalogs` returns 10 readable
catalogs. Readable must be ≥ manageable; `readable` probably counts only `is_public=false` or only explicit ACLs.

### ⚠️ 5. Dataverse-imported acquisitions point at a Compose hostname

The 6 FCHPT entries have `storage_backend=external_url`, `file_url=http://dataverse:8080/api/access/datafile/<n>`.
`dataverse` resolves only on the old Compose network; dev moved to k8s on 2026-11-07, so these downloads are
dead (and would be from any browser anyway — an internal host is handed to readers as a redirect target).
They were the only `external_url` fixtures on dev.

### ⚠️ 6. LCP-enabled entry with no file reports "available now"

"Szepesi Lapok" (`641436da-…`): `readium_enabled=true`, zero acquisitions, `availability: available_now 1/1`.
A loan cannot be issued for something that has no file; availability should consider whether an encrypted
publication exists.

### ⚠️ 7. Feed `url_name` values are malformed UUIDs

Several feeds carry `url_name` like `fb2aa269-0d281-244e8-7b7b3-a1e7de9ab0a69` (5/5/5/5/13 segments), next to
valid UUIDs. Some client generates them (looks like a broken JS random-UUID); a slug field should hold a slug.

### ℹ️ 8. Category scheme `"nan"`

"Another test category" has `scheme: "nan"` — a pandas NaN serialized into a string by some import path.

### ℹ️ 9. Feed "IVZ" has no parent

Acquisition feed with 1 entry and no parent → unreachable from navigation.

### ℹ️ 10. Empty strings vs NULL in file fields

Dataverse-imported entries store `image = ''` and `acquisition.content = ''`; others use NULL. Code checks
`if not acquisition.content` so it works, but queries for "has a cover" disagree depending on which is used.

### ℹ️ 11. Local `.mcp.json:88`

`"env": { "ELVIRA_API_KEY": "${}" }` on the `elvira-dev` entry — works only because the header reads
`${ELVIRA_DEV_API_KEY}` from the parent environment.

### ⚠️ 12. Encryption status never reaches `registered` although the LCP server has the content

"wienvqevc": `encrypted_contents.status = pending` since 2026-07-19, `encrypted_at`/`registered_at` NULL — yet
`readium-lcp-dev.content` holds `b1f1f63b-…` (3 054 823 B). The worker encrypted and registered it; the callback
to Elvira was lost. `list_encryption_jobs` would offer it for re-queue, which would encrypt it a second time.
Meanwhile `search_entries` reports the entry `available_now`, so availability ignores the job status too.

### ⚠️ 13. Extraction run stuck in `running`

"Test Book" acquisition: `extraction_run 588c0818-…` `status=running` since 2026-11-04 17:19, `paragraphs: pending`,
`finished_at` NULL. No watchdog marks it failed.

### 🔴 14. Book files missing on the dev volume (the reverse of orphans: rows without files)

Enabling LCP on "Particle Accelerator Physics" (`822a2bf7-…`) failed at once:
`Failed to insert license page: [Errno 2] No such file or directory:
'/var/local/evilflowers/catalogs/openresearchlibrary/822a2bf7-…/e0435d56-….pdf'`.
Probing every download URL of the catalog (`Range: bytes=0-0`, 406 entries, 1 218 URLs):

| | 200 | 404 | 403 | 503 |
|---|---|---|---|---|
| acquisition | 342 | **49** | 3 (IP-blocked titles — correct) | 11 |
| cover | 347 | **49** | – | 10 |
| thumbnail | 395 | 0 | – | 11 |

The same 49 entries miss both book and cover while their thumbnails exist — a partial copy, not files that never
existed. The 503s came in one burst (with connection refusals minutes earlier): dev restarted under the probe load. Either the files never reached the old Compose
volume, or the 2026-11-07 copy into the k8s `data` PVC was incomplete. Nothing in the catalog notices: the entries
list, search and OPDS feeds advertise downloads that 404.

### ⚠️ 15. Availability ignores the encryption state

Right after `update_entry(lcp_enabled=true, lcp_copies=5)` the response said `available_now 5/5` — before the job
was registered. "Particle Accelerator Physics" kept reporting availability with its job in `failed`, and
"wienvqevc" (issue 12) with its job `pending` for four months. `lcp_state` should be "not lendable" until the
`encrypted_contents` row is `registered`.

### ⚠️ 16. A failed encryption job blocks re-encryption by toggle

`apps/readium/signals.py:259` skips an acquisition when `hasattr(acquisition, "encrypted_content")` — a `failed`
row counts. Turning LCP off and on again never retries; only the admin `requeue_encryption` does. The UI gives an
editor no hint.

### ⚠️ 17. Every MCP feed write reports failure although it succeeded

`create_feed` (and any tool returning a feed resource link) fails client-side with the same
`name: expected string, received object` as issue 2, **after** the server committed the write. An agent will retry
and hit the `url_name` uniqueness check, or give up believing nothing happened. All 9 feeds created in this session
were verified in SQL instead.

### ⚠️ 18. Absolute URLs are generated as `http://`

`cover_url`, `download_url`, feed `url` and acquisition `url` all start with `http://dev.evilflowers…` behind the
TLS edge (nginx → Envoy → pod). The proxy scheme header is not trusted (`SECURE_PROXY_SSL_HEADER` /
`USE_X_FORWARDED_HOST`) or not forwarded by Envoy since the k8s move. Clients follow a redirect at best, mixed
content at worst; OPDS readers may refuse.

### ℹ️ 19. MCP `update_entry` cannot set viewer options

Only `lcp_enabled` / `lcp_copies` are exposed. `evilflowers_ip_block`, `viewer_print`, `share_enabled`,
`annotations_create`, `render_type`, OCR flags need REST `PUT` (which, good news, merges `config` and leaves
authors/categories/feeds/summary intact when omitted — verified).

### ℹ️ 20. Metadata quality of the ORL import

- German titles carry `language: en` (e.g. "Deutsch Als Zweitsprache", "Altern Im Wandel", "Weißbuch …").
- Many "summaries" are keyword lists ("robotics; autonomous mobile robots; algorithms; …").
- Dozens of entries have no author; "Iutam : a Short History" has a 9-character summary.
- "CHAPTER 5 'Rapt Up with Joy':" is a book chapter, not a publication.
- "Generating PlantUML …" (Kordoš) is `language: sk` with an English title and no summary.
- Titles keep the source's broken capitalisation ("integrating Immigrants", "UKrainian Migration").

## Orphan check

### Before deletion (2026-11-08)

LCP server (`readium-lcp-dev.content`): 14 rows, 1:1 with `evilflowers_dev.encrypted_contents` — **no LCP orphans
from past deletions**.

Storage paths owned by the entries deleted below (relative to the dev `data` PVC storage root):

| Entry | Paths |
|---|---|
| Test Book `45d89158-…` | `catalogs/openresearchlibrary/45d89158-d087-441e-9ce8-0340f0ee0efe/` → `cover_Gal2GMU.jpg`, `thumbnail_hYXB5MY.jpg`, `df7b91a8-8ae9-4c8e-ac96-835db7774df6.pdf`, `extraction/e06ce850-1d1d-4c9c-ae63-c51dedf38903/` |
| wienvqevc `40ff1b6e-…` | `catalogs/openresearchlibrary/40ff1b6e-a1aa-4f22-983d-50fd551ad907/` → `cover_9ErvUNa.jpg`, `thumbnail_MvM26iD.jpg`, `efaf479d-5143-4f19-b509-457639da5bd2.pdf`, `encrypted/b1f1f63b-4461-463b-a89a-efaac52354ba.lcpdf`; LCP server content `b1f1f63b-4461-463b-a89a-efaac52354ba` |
| 6 × FCHPT | `catalogs/fchpt/<entry-id>/thumbnail.jpg` (payload is an external Dataverse URL, nothing local) |

### After deletion

- DB rows: entries, acquisitions, `encrypted_contents`, `extraction_run` rows are gone (Django cascade).
  `assistant_chats.entry_id` is nullable — 5 chats now point at no entry (one of them belonged to a deleted FCHPT
  entry). Harmless, but the chats survive their publication.
- **LCP server orphan, confirmed**: `readium-lcp-dev.content` still holds `b1f1f63b-4461-463b-a89a-efaac52354ba`
  (wienvqevc). Its `location`
  `https://dev.evilflowers.elvira.stuba.sk/readium/v1/content/b1f1f63b-….lcpdf` now answers **404**. A licence
  request for that content id would hand a reader a dead link. The LCP server now holds 14 content rows for 13
  Elvira records — 1 orphan.
- **Storage**: not reachable from this session (no SSH to control, no kube tunnel). The dev storage root is
  `/var/local/evilflowers` (from the encryption error of issue 14). Every path in the table above is expected to
  still exist:

  ```bash
  kubectl -n evilflowers-dev exec deploy/evilflowers -- ls -la /var/local/evilflowers/catalogs/fchpt \
    /var/local/evilflowers/catalogs/openresearchlibrary/45d89158-d087-441e-9ce8-0340f0ee0efe \
    /var/local/evilflowers/catalogs/openresearchlibrary/40ff1b6e-a1aa-4f22-983d-50fd551ad907
  ```

  Once the fixes are deployed, `python manage.py storage_audit` on the pod lists these orphans (and the 49 missing
  files) and `--delete-orphans` removes them.

## Clean-up log

### Deleted entries (REST `DELETE /api/v1/catalogs/{c}/entries/{e}`, all 204)

| Catalog | Entry | Why |
|---|---|---|
| ORL | Test Book `45d89158-…` | UX test upload, author "UX Testing" |
| ORL | wienvqevc `40ff1b6e-…` | keyboard mash, author "jaro ja", publisher "aecacv" |
| FCHPT | sadsd asd, fdfs, ddsa, ddsaasa, adasdasdasd, asdasdasd | Dataverse import test, dead `http://dataverse:8080` links |

Left in place for you to decide:

| Entry | Why it is borderline |
|---|---|
| Generating PlantUML from UML class and sequence diagram images… `809c0fbd-…` (Tomáš Kordoš, 2026-11-04) | probably a real paper; `language: sk` on an English title, no summary, in no feed |
| BP: IET Instrukcie `dc361c22-…` | internal bachelor-project instructions, LCP-registered, popularity 56 |
| Szepesi Lapok `641436da-…` | real 1912 periodical but **no file**; LCP switched off |

### Feeds

Deleted: "Test", "Test navigation", "nova zbierka", "nova zbierka 2", "E - Spoločenské vedy" (empty, under "Test"),
"IVZ" (only held a deleted test entry, no parent) in ORL, and "Bakalar" (empty) in STU FIIT. The FIIT course tree
(Bakalárske/Inžinierske štúdium → Predmety → course codes) is kept.

Added in ORL (every write verified in SQL — the MCP reported each as failed, issue 17):

| Feed | Kind | Content |
|---|---|---|
| Podľa oblasti / By subject (`podla-oblasti`) | navigation, top level | parent of the eight below |
| Informatika a technika | acquisition | MDT 004, 62 — 64 titles |
| Prírodné vedy a matematika | acquisition | MDT 51–57 — 38 |
| Medicína a zdravie | acquisition | MDT 61 — 51 |
| Životné prostredie a poľnohospodárstvo | acquisition | MDT 502/504, 63 — 54 |
| Spoločenské vedy a právo | acquisition | MDT 31, 314, 32, 34 — 69 |
| Ekonomika a manažment | acquisition | MDT 33, 005 — 44 |
| Vzdelávanie | acquisition | MDT 37 — 44 |
| Humanitné vedy | acquisition | MDT 0, 02, 1, 7, 8, 9 — 42 |
| Chránené tituly (Readium LCP) (`readium-lcp`) | acquisition, top level | all 17 LCP titles |

### Categories

Deleted "Test category", "Another test category" ("RBAC bro", scheme `nan`) and "Geofyzika" (on a QUIC paper).
Created 24 MDT terms in ORL in the same format as the STU vocabulary (term = MDT code, scheme `MDT`, sk + en
labels) and filed all 406 entries under exactly one each — keyword rules on the title, every unmatched or
doubtful title decided by hand (~80). First pass by Claude, no librarian review.

### Readium LCP test spread (all `registered`)

| Configuration | Title |
|---|---|
| 1 copy, defaults | 8 pre-existing titles (Freshwater Governance, Interferometry, Managing Protected Areas, Ocean-Atmosphere, Self-Reported Population Health, Synchronized Factories, Mathematics Education, Ukrainian Migration) |
| 1 copy, print/share/annotations off | Library and Information Sciences |
| 1 copy, printing off (pre-existing variants) | QUIC; Fingerprint Biometrics; Wisense |
| 1 copy + IP block | Handbook of Ocean Wave Energy; BP: IET Instrukcie (pre-existing) |
| 2 copies | Optics in Our Time |
| 3 copies | South-North Migration of EU Citizens (pre-existing) |
| 5 copies | Elements of Robotics |
| IP block **without** LCP | Opening Science |
| failed job (file missing), LCP off again | Particle Accelerator Physics |

No "fully borrowed" title: that needs real loans.

### Metadata

23 German titles `en` → `de`; "Generating PlantUML…" `sk` → `en`.
