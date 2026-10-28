---
draft: true
date: 2026-10-27
authors:
  - kordostomas
categories:
  - Feature
tags:
  - i18n
  - feeds
  - categories
  - api
  - opds
---

# IP-017: Multilingual Feed Titles and Category Labels

Store feed (collection) titles and category labels in several languages instead of one string.
The value becomes a JSON object keyed by ISO 639-1 code (`{"sk": "Informatika", "en": "Computer
science"}`). The API always sends the whole object, and clients pick the translation for their
UI language. Administrators choose which languages can be entered.

<!-- more -->

## Status

**Status**: Draft
**Last Updated**: 2026-10-27
**Implementation**: Complete (backend + elvira-portal); migration, tests and UI not yet run

## Problem Statement

elvira-portal is bilingual (Slovak and English UI), but the content that names its navigation is
not. `Feed.title` (`CharField(100)`) and `Category.label` (`CharField(255)`, nullable) hold one
string each, so `/feeds` and `/categories` on elvira.stuba.sk show Slovak names to English
readers.

- An English reader browsing collections or categories sees only Slovak names.
- Admins cannot enter a second language anywhere; a workaround such as `"Informatika / Computer
  science"` breaks search, sorting and OPDS clients.
- Which languages to offer is a deployment decision (another faculty may want Czech or German),
  so it should not be hard-coded in the frontend.

## Proposed Solution

### Overview

1. Change `Feed.title` and `Category.label` to `JSONField` holding a *localized text*: a mapping
   from ISO 639-1 code to text. An empty mapping (`{}`) means "no value".
2. Slovak (the default language) is the primary language: every non-empty value must contain
   it, other translations are optional, and it cannot be disabled.
3. Migrate every existing string to `{"<default language>": "<old text>"}`, where the default
   language is a new setting `EVILFLOWERS_DEFAULT_LANGUAGE` (default `sk`).
4. The REST API always returns the full object. Writes accept the object, or a plain string
   (stored under the default language) for backwards compatibility.
5. Add `Language.translatable`. Superusers toggle it from a new elvira-portal admin page; the
   feed and category editors show one input per translatable language.
6. Formats that carry one title (OPDS 1.2, OPDS 2.0, facets) pick a translation server-side
   using the client's `Accept-Language`.
7. `order_by=title` (feeds) and `order_by=label` (categories) sort alphabetically by the
   translation in the client's UI language, using the same fallback.

### Fallback rule

Used by both the backend (`localize()`) and the frontend (`localize()`):

1. The requested language (UI language or `Accept-Language`).
2. The default language (`EVILFLOWERS_DEFAULT_LANGUAGE` / `DEFAULT_CONTENT_LANGUAGE`, both `sk`).
3. English (frontend only).
4. Any non-empty translation.

### Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| Localized text helpers | `apps/core/fields/localized.py` | `normalize_localized`, `localize`, `request_language`, `LocalizedValues` (search expression), `LocalizedTextFormField` |
| Migration | `apps/core/migrations/0042_localized_titles.py` | Column type change + data conversion, `Language.translatable` |
| Language admin API | `apps/api/views/languages.py`, `apps/api/filters/languages.py`, `apps/api/forms/languages.py` | `translatable` filter, superuser-only `PUT /api/v1/languages/<id>` |
| Frontend helpers | `elvira-portal/src/utils/func/localized.ts` | `localize`, `useLocalize`, `cleanLocalized`, `localizedIncludes` |
| Multilingual input | `elvira-portal/src/components/admin/LocalizedTextInput.tsx` | One input per translatable language |
| Languages page | `elvira-portal/src/pages/admin/AdminLanguages.tsx` | Admin → Settings → Languages |

### Architecture

```
Admin (Languages page) ──PUT translatable──▶ Language.translatable
                                                   │
Admin (Feed/Category drawer) ◀──GET ?translatable=true
        │ {"sk": "...", "en": "..."}
        ▼
FeedForm / CategoryForm (LocalizedTextFormField) ──▶ Feed.title / Category.label (jsonb)
                                                          │
          ┌───────────────────────────────────────────────┼─────────────────────────────┐
          ▼                                               ▼                             ▼
REST API (full object)                       OPDS 1.2 / 2.0 (one string,      MCP tools (full object;
  └▶ elvira-portal localize(UI language)       Accept-Language + fallback)      string or object input)
```

## Implementation Plan

### Phase 1: Storage and migration

- [x] Add `EVILFLOWERS_DEFAULT_LANGUAGE` setting (env, default `sk`)
- [x] `Feed.title` → `JSONField(default=dict)`; `Category.label` → `JSONField(default=dict, blank=True)`
- [x] Drop the B-tree indexes on `categories.label` and `(catalog_id, label)`
- [x] Migration `0042_localized_titles`: `ALTER COLUMN ... TYPE jsonb USING jsonb_build_object(...)`,
      empty/NULL → `{}`, `NOT NULL`; reverse migration keeps the default-language text
- [x] Add `Language.translatable`; migration and `setup` enable the default language and English

### Phase 2: API

- [x] `LocalizedTextFormField` accepting a string or an object; validates `[a-z]{2}` keys and per-translation `max_length`
- [x] A non-empty value must include the default language (Q5)
- [x] `PUT /api/v1/languages/<id>` refuses to disable the default language (422)
- [x] Localized sorting: `localized_sort_key()` annotation + `Ordering` alias (`title` → `title_sort`,
      `label` → `label_sort`), language from `?language=` or `Accept-Language` (Q2)
- [x] `FeedForm.title` and `CategoryForm.label` use it
- [x] Serializers return `Dict[str, str]`; `LanguageSerializer` exposes `translatable`
- [x] Feed `title` and category `label`/`query` filters search all translations (`jsonb_path_query_array(col, '$.*')::text` + `unaccent`)
- [x] Entry search and facets: `categories__label__icontains` on jsonb; facet title localized
- [x] `GET /api/v1/languages` gains `translatable` and `query` filters; new `PUT /api/v1/languages/<id>` (superuser)
- [x] OpenAPI documents the localized field as `object` with string values
- [x] Entry import (`categories` records) stores `{}` instead of `NULL` for a missing label

### Phase 3: OPDS and MCP

- [x] OPDS 1.2 navigation/acquisition feed titles and category labels localized by `Accept-Language`
- [x] OPDS 2.0 navigation titles and subjects localized; navigation sorted by localized title in Python
- [x] MCP `list_feeds` sorted by the localized title (default language)
- [x] MCP: `Arguments.localized()`; create/update feed and category tools accept a string or an object;
      projections and output schemas return the object; messages use the localized string
- [x] Update MCP test fixtures and assertions to the object form

### Phase 4: elvira-portal

- [x] `LocalizedText` type; `ICategory.label`, `IFeed.title`, `IFeedDetail.title` use it
- [x] `localize()` / `useLocalize()` at every display site (cards, breadcrumbs, suggestions, filters,
      entry detail and list, admin tables, autofills)
- [x] `LocalizedTextInput` in the feed and category drawers; Slovak input always first and required;
      keeps translations in languages that were disabled later
- [x] Feed/category lists send `language=<UI language>` so `order_by=title|label` sorts in that language
- [x] Languages page keeps Slovak on top, switched on and locked
- [x] Admin → Settings → Languages page with search and per-language switch (React Query, cache invalidated on change)
- [x] SK/EN strings for the new page and field hints

### Phase 5: Verification

- [ ] Run migration `0042` forwards and backwards on a copy of production data
- [ ] Run the backend test suite (MCP tests were updated but not run)
- [ ] Add tests for `LocalizedTextFormField`, `localize`, the language endpoints and the localized filters
- [ ] Manual UI check: edit a feed/category in two languages, switch UI language, toggle a language

## Technical Details

### Data model

```python
class Feed(BaseModel):
    # Localized text, e.g. {"sk": "Nové knihy", "en": "New books"}
    title = models.JSONField(default=dict)

class Category(BaseModel):
    # Localized text, e.g. {"sk": "Informatika", "en": "Computer science"}
    label = models.JSONField(default=dict, blank=True)

class Language(BaseModel):
    translatable = models.BooleanField(default=False)
```

`Feed.Meta.unique_together = (("catalog", "title"), ...)` is unchanged and now compares whole
jsonb objects (see Q1).

### Migration SQL (forward)

```sql
ALTER TABLE "feeds" ALTER COLUMN "title" TYPE jsonb USING (
    CASE WHEN "title" IS NULL OR btrim("title") = '' THEN '{}'::jsonb
    ELSE jsonb_build_object('sk', "title") END
);
ALTER TABLE "feeds" ALTER COLUMN "title" SET NOT NULL;
```

The reverse uses `COALESCE(col ->> 'sk', jsonb_path_query_first(col, '$.*') #>> '{}')`, truncated
to the old column length (subqueries are not allowed in `USING`).

### API

Read (feeds, categories, embedded in entries):

```json
{"id": "…", "term": "informatics", "label": {"sk": "Informatika", "en": "Computer science"}}
```

Write — both forms are accepted; a string is stored under the default language:

```json
{"title": {"sk": "Nové knihy", "en": "New books"}}
{"title": "Nové knihy"}
```

Blank translations are dropped. An update replaces the whole object. A non-empty value must
contain the default language (`sk`), otherwise the form returns `default_language`:
`{"en": "New books"}` is rejected. The feed title is required; the category label is optional,
but if any translation is given, Slovak must be one of them.

Languages:

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/v1/languages?translatable=true&paginate=false` | Languages offered in the editors |
| PUT | `/api/v1/languages/<uuid>` `{"translatable": true}` | Superuser only; 403 otherwise; 422 when disabling the default language |

### Sorting

List endpoints annotate a sort key and alias the public column name to it:

```python
feeds.annotate(title_sort=localized_sort_key("title", sort_language(request)))
Ordering.create_from_request(request, aliases={"title": "title_sort"})
```

`localized_sort_key` renders
`lower(COALESCE(NULLIF(title ->> '<lang>', ''), NULLIF(title ->> 'sk', ''), <first translation>[, term]))`.
Categories fall back to `term` when they have no label. `sort_language` reads `?language=`
(elvira-portal sends its UI language) and otherwise `Accept-Language`. Order within a language
follows the database collation.

### Search

`LocalizedValues("title")` renders `jsonb_path_query_array("title", '$.*')::text`, i.e. only the
values, so `unaccent__icontains` does not match language codes. Entry full-text search uses
Django's jsonb `icontains`, which casts the whole object to text (see Q9).

## Alternatives Considered

| Alternative | Why not |
|-------------|---------|
| `django-modeltranslation` (`title_sk`, `title_en` columns) | A schema migration for every new language; conflicts with admin-configurable languages |
| Separate `Translation` table (`object_id`, `field`, `language`, `text`) | A join on every list and filter; heavier than needed for two short fields |
| Return only the requested language (`?lang=en`) | Admin editors need every translation; caching per language; the frontend could not switch language without refetching |
| Keep `title` as a string and add `title_i18n` | Two sources of truth that drift; every client must know which one wins |
| Hard-code `sk`/`en` in the frontend | Another deployment could not add a language without a frontend release |

## Trade-offs and Risks

- **Breaking API change.** `title`/`label` change from string to object. Backend and elvira-portal
  must be deployed together; third-party API consumers reading these fields break (writes keep
  working through the string fallback).
- **Uniqueness is weaker.** `(catalog, title)` now compares whole objects, so `{"sk": "X"}` and
  `{"sk": "X", "en": "Y"}` are not duplicates (Q1).
- **Old catalog dumps.** `dump_catalog` archives made before `0042` contain strings; `load_catalog`
  would write JSON strings that the serializer rejects (Q3).
- **Default language in two places.** Backend setting and frontend constant must agree (Q4).
- **Slovak is mandatory.** API clients that send only an English title now get a validation error.
- **Collation.** Sorting uses the database collation; with a non-Slovak collation, `ch` sorts
  under `c` rather than after `h`.
- **Dropped indexes.** The label B-tree indexes were removed; label search was already `icontains`,
  which did not use them.

## Success Criteria

- Existing feeds and categories show unchanged Slovak names after migration.
- An admin can enter a title in Slovak and English; English UI shows the English one, Slovak UI the Slovak one.
- A missing translation falls back to Slovak, never to an empty label.
- Enabling a language on the Languages page adds an input for it in both editors without a reload.
- Searching for a feed or category by either translation finds it.
- OPDS clients with `Accept-Language: en` get English titles where present.

## Future Considerations

- Localize other user-facing text: `Feed.content`, `Catalog.title`.
- A default-language selector in the Languages page instead of an environment variable.
- Expose the configured default language via the API so the frontend does not hard-code it.
- Per-language collation for sorting (e.g. `COLLATE "sk-x-icu"` when ICU is available).

## References

- Implementation: `apps/core/fields/localized.py`, `apps/core/migrations/0042_localized_titles.py`
- elvira-portal: `src/utils/func/localized.ts`, `src/components/admin/LocalizedTextInput.tsx`, `src/pages/admin/AdminLanguages.tsx`
- [IP-014: Model Context Protocol Server](ip-014-model-context-protocol-server.md) — MCP tools updated here
- [PostgreSQL SQL/JSON path functions](https://www.postgresql.org/docs/current/functions-json.html)

## Review Questions

**Status**: ⏳ Awaiting Answers
**Review Date**: 2026-10-27
**Reviewer**: Claude AI

The following questions must be answered before the proposal is accepted:

---

### Q1: Feed title uniqueness on jsonb 🔴 Critical

**Issue**: `Feed.Meta.unique_together` still contains `("catalog", "title")`. With jsonb this compares
whole objects, so two feeds named "Nové knihy" in Slovak are allowed as soon as one also has an
English title. The REST endpoint never checked title uniqueness (MCP's `_assert_unique` does,
with the same whole-object comparison).

**Context**: Duplicate names confuse readers in navigation; on the other hand a DB-level unique
constraint on one translation needs an expression index.

**Question**: What should be unique within a catalog?

**Options**:
- [ ] **A**: Drop the `(catalog, title)` constraint; `url_name` stays the unique key (recommended)
- [ ] **B**: Unique per translation: expression index on `(catalog_id, title ->> 'sk')` and validation for every language in the form
- [ ] **C**: Keep the whole-object constraint as implemented

**Answer**:
```
```

**Resolution**:
```
```

---

### Q2: Sorting by title / label ⚠️ Medium ✅ Resolved

**Issue**: `order_by=title` (feeds) and `order_by=label` (admin categories table) ordered jsonb
objects, which PostgreSQL compares by key count, then keys, then values — not alphabetically in
the reader's language. OPDS 2.0 navigation was changed to sort in Python; the REST API was not.

**Context**: Admin tables and any client relying on `order_by=title` get a stable but non-alphabetical order.

**Question**: How should the API sort localized fields?

**Options**:
- [ ] **A**: Add an `Ordering` alias mapping `title`/`label` to `title ->> <default language>` (recommended)
- [x] **B**: Sort by the requested language (`order_by=title&lang=en`)
- [ ] **C**: Leave as is

**Answer**:
```
B — sorting must work. (kordostomas, 2026-10-27: "for sure we want sorting working")
```

**Resolution**:
```
Implemented: localized_sort_key() annotation (requested language → sk → any translation,
lower-cased; categories fall back to term) aliased via Ordering for order_by=title|label.
Language comes from ?language= (elvira-portal sends its UI language) or Accept-Language.
MCP list_feeds sorts by the default-language title.
```

---

### Q3: Loading catalog dumps made before migration 0042 ⚠️ Medium

**Issue**: `load_catalog` deserializes `entities.json` with Django's serializer. Older dumps contain
`"title": "Nové knihy"`, which becomes a JSON *string* in the jsonb column. Such rows break
`FeedSerializer` (`Dict[str, str]`) and `localize()` handles them only by accident.

**Context**: Dumps are used for backups and moving catalogs between instances.

**Question**: Should `load_catalog` convert old dumps?

**Options**:
- [ ] **A**: Normalize `Feed.title` / `Category.label` with `normalize_localized` while loading (recommended)
- [ ] **B**: Document that pre-0042 dumps must be loaded into a pre-0042 database and migrated
- [ ] **C**: Add a CHECK constraint `jsonb_typeof(title) = 'object'` and let loading fail loudly

**Answer**:
```
```

**Resolution**:
```
```

---

### Q4: Default language defined twice ⚠️ Medium

**Issue**: The backend uses `EVILFLOWERS_DEFAULT_LANGUAGE` (env), the frontend hard-codes
`DEFAULT_CONTENT_LANGUAGE = 'sk'` in `localized.ts`. A deployment that sets the env to `cs` would
store migrated titles under `cs` while the frontend falls back to `sk`, then `en`, then "any" —
it still finds a value, but the editor lists `sk` first.

**Question**: Where should the frontend get the default language?

**Options**:
- [ ] **A**: Expose it from the backend (e.g. `GET /api/v1/status` or a `default` flag on languages) (recommended)
- [ ] **B**: Vite env variable `ELVIRA_DEFAULT_CONTENT_LANGUAGE`
- [ ] **C**: Keep `sk` hard-coded; STU is the only deployment

**Answer**:
```
```

**Resolution**:
```
```

---

### Q5: Must the default-language translation be present? ⚠️ Medium ✅ Resolved

**Issue**: Validation required only *some* non-empty translation. A feed could be saved with only
`{"en": "New books"}`; OPDS and Slovak UI then fell back to English.

**Question**: Should the default language be mandatory?

**Options**:
- [x] **A**: Yes — `FeedForm`/`CategoryForm` reject values without the default language; editor marks it required (recommended)
- [ ] **B**: No — any translation is enough

**Answer**:
```
A — Slovak is primary; never allow English without Slovak. (kordostomas, 2026-10-27)
```

**Resolution**:
```
Implemented: LocalizedTextFormField rejects a non-empty value without the default language
(code default_language), which covers REST and MCP. The editors always show the Slovak input
first, mark it required and validate it. The Languages page and PUT /api/v1/languages/<id>
do not allow disabling the default language.
```

---

### Q6: Which language codes may be stored? ℹ️ Low

**Issue**: `LocalizedTextFormField` validates only the format `[a-z]{2}`. A client can store `"xx"`
or a language that is not `translatable`. The editor shows such keys so they are not lost.

**Question**: Should keys be checked against the `languages` table?

**Options**:
- [ ] **A**: Must exist in `languages` (any, not only translatable) (recommended)
- [ ] **B**: Must be `translatable`
- [ ] **C**: Format only (as implemented)

**Answer**:
```
```

**Resolution**:
```
```

---

### Q7: What happens to translations when a language is disabled? ℹ️ Low

**Issue**: Disabling a language only hides it from new editor inputs. Stored translations remain,
are still returned by the API, and are still shown to readers whose UI uses that language.

**Question**: Is "disable = stop offering, keep serving" the intended meaning?

**Options**:
- [ ] **A**: Yes, keep and serve (as implemented) (recommended)
- [ ] **B**: Keep but stop serving (filter API output to translatable languages)
- [ ] **C**: Offer to delete the translations when disabling

**Answer**:
```
```

**Resolution**:
```
```

---

### Q8: OPDS language negotiation ℹ️ Low

**Issue**: OPDS views call `get_language_from_request`, which needs no `LocaleMiddleware` but is
limited to `settings.LANGUAGES` (Django's full default list). Facet titles and OPDS 1.2 category
labels have no request in scope and always use the default language.

**Question**: Is per-request localization needed for facets and entry categories?

**Options**:
- [ ] **A**: Pass the request language through `EntryFilter` facets and `AcquisitionEntry` (recommended)
- [ ] **B**: Default language is fine for those

**Answer**:
```
```

**Resolution**:
```
```

---

### Q9: Entry full-text search matches language codes ℹ️ Low

**Issue**: Entry search uses `categories__label__icontains`, which casts the whole jsonb to text,
so a search for "en" or "sk" matches every category with that key.

**Question**: Switch entry search to `LocalizedValues` like the category/feed filters?

**Options**:
- [ ] **A**: Yes, annotate with `LocalizedValues("categories__label")` (recommended)
- [ ] **B**: No, two-letter queries are rare

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
| 2026-10-27 | kordostomas | Initial draft documenting the implemented change: jsonb `Feed.title` / `Category.label`, migration 0042, `Language.translatable` + admin page, OPDS/MCP localization, elvira-portal display and editors |
| 2026-10-27 | kordostomas | Added Review Questions section |
| 2026-10-27 | kordostomas | Resolved Q2 (sort by requested language, implemented) and Q5 (Slovak required and cannot be disabled, implemented) |
