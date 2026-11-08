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
  request for that content id would hand a reader a dead link. 15 → 14 Elvira records, still 14 LCP rows... now
  13 + 1 orphan.
- **Storage**: not reachable from this session (no SSH to control, no kube tunnel). Verify on the pod; every path in
  the table above is expected to still exist:

  ```bash
  kubectl -n evilflowers-dev exec deploy/evilflowers -- sh -c \
    'cd "$(python -c "from django.conf import settings; print(settings.MEDIA_ROOT)" 2>/dev/null || echo /usr/src/app/private)"; \
     ls -la catalogs/fchpt catalogs/openresearchlibrary/45d89158-d087-441e-9ce8-0340f0ee0efe \
            catalogs/openresearchlibrary/40ff1b6e-a1aa-4f22-983d-50fd551ad907'
  ```

  (adjust the storage root if `MEDIA_ROOT` is not where `get_storage()` writes.)

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
| Szepesi Lapok `641436da-…` | real 1912 periodical but **no file**, LCP enabled (issue 6) |
