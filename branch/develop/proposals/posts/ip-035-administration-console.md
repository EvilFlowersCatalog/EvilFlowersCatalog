---
draft: true
date: 2026-11-17
authors:
  - jdubec
categories:
  - Administration
  - Operations
tags:
  - django-admin
  - console
  - static-files
  - whitenoise
  - design
  - data-grid
  - graph
  - vectors
  - jobs
---

# IP-035: The Administration Console — Django Admin for the Whole Database, in Switchable Modules

The Django admin at `/admin/` had broken styles on dev and prod and covered 17 of 63 models. This proposal
turns it into an **administration console**: every table of the catalog is reachable, grouped by the work it
serves into **modules** that can be switched on and off at runtime, dressed in the design of
[elvira.digital](https://elvira.digital), and extended with interactive pages (data grid, knowledge-graph
explorer, vector explorer, live jobs dashboard) that need no JavaScript build step.

<!-- more -->

## Status

📝 **Draft** (2026-11-17). Implemented and tested locally, uncommitted: the full suite runs 2243 tests with
one failure in untouched tracing code (see Changelog); 91 console tests cover coverage, gating, CRUD POSTs and
the grid. The Docker image builds with the new `collectstatic` step and its gunicorn serves
`/static/admin/css/base.css` with 200 (gzip) without a database. Overview, data grid, Jobs, Graph, Vectors,
the Entry change view and the catalog/author/entry add flows were clicked through in a browser (Playwright)
on production settings; Modules "Save" and the Jobs actions are covered by tests only. Not deployed. Q1 is
answered (full CRUD); Q2–Q5 are open and implemented with their recommended options.

## Problem Statement

1. **Styles were broken everywhere.** `GET /static/admin/css/base.css` answered **404 on dev and prod**
   (checked 2026-11-17). Nothing ran `collectstatic`, and `/static/` was routed only under `DEBUG`, which is
   off in both deployments. The admin rendered as unstyled HTML.
2. **Most of the database was invisible.** 17 models were registered, 46 were not — among them `User`,
   licences, reservations, the jobs ledger, harvesting, content policies and all of OAI. Operators fell back to
   `psql`.
3. **Unsafe defaults where it was registered.** Plain `ModelAdmin`s made every row editable (an `Entry` saved
   from a bare form skips search vectors, OAI records and the author-link rules), foreign keys rendered as
   `<select>`s (the extraction tables hold 400 000 paragraphs and 2.5 million links), and pgvector columns were
   loaded into lists and forms.
4. **No structure.** Django groups models by app label (`core`, `readium`, `decider`), not by the work a
   librarian or operator does, and offers no way to hide sensitive tables (reading history, chat transcripts,
   disclosure logs) from a deployment.
5. **No look.** The stock blue Django admin has nothing of the ELVIRA identity.

## Proposed Solution

### Brainstorm: what to expose, and how

| Question | Options considered | Chosen |
|---|---|---|
| Organise by | Django app · data owner · **the work it serves** | Functional modules across apps (Library, People & access, Lending, Jobs, Extraction, Knowledge, Content access, Assistant, Interoperability, Notifications, System, + three sensitive ones) |
| Switch modules | Code (`INSTALLED_APPS`) · env only · **env default + runtime setting** | `EVILFLOWERS_ADMIN_MODULES` is the deployment default, the instance setting `admin.modules` (#20 settings store) overrides it at runtime; a module whose feature flag is off (assistant, extraction) is never shown |
| Write access | Everything editable · nothing · **opt-in per model** | Full CRUD for the library (catalogs, entries, authors, categories, feeds, files, prices), accounts, reference data, harvest sources, content policies, notifications and settings (Q1); machine-written state (LCP licences and encryption, jobs ledger, extraction, decider output, OAI records, readers' activity) stays read-only; `EVILFLOWERS_ADMIN_READ_ONLY` turns every write and action off for a deployment |
| Writes that have services | Bare forms · **the same rules as REST** | Side effects are model signals (search vectors, OAI, harvest sync, file cleanup, LCP/extraction queues), so an admin save triggers what a REST save does; the extra REST rules are repeated: creator's `manage` grant, entry duplicate rule, feed slug rule, covers via `set_cover`, files via `attach_acquisition`, policies via `policies.save`, harvest config via the adapter registry, reviews via `ReviewService`, settings via the registry, jobs via the IP-031 facade |
| Personal data | Always visible · **off by default, switched on deliberately** | `reading`, `conversations`, `audit` modules default off; the switch is a stored setting with `updated_by` |
| Theme | Third-party skin (Unfold, Jazzmin, Grappelli) · **own CSS over Django's variables** | Django admin's CSS custom properties remapped to the `--e-*` tokens of elvira.digital, light and dark; no dependency on a skin's release cycle |
| Front end | SPA (React/Vue + build) · htmx · **Alpine.js + vendored libraries** | Alpine.js 3 for components; Tabulator (grid), Cytoscape.js (graph), ECharts (charts) — vendored, pinned, served by WhiteNoise; no Node in the image, no CDN at runtime |
| Static files | nginx `location /static` · S3 · **WhiteNoise in the web process** | Same answer for Compose prod and k8s dev, no IaC change; `collectstatic` at image build |

### Architecture

```
apps/console/
  modules.py      Module registry, defaults, three-layer switch, EXCLUDED_MODELS, `admin.modules` setting
  options.py      ConsoleMixin/ConsoleModelAdmin (gating, read-only default, big tables, vectors, raw-id FKs),
                  EstimatedCountPaginator, @register(Model, module=...)
  site.py         ElviraAdminSite: module grouping, attention counters, planner row estimates, console URLs
  apps.py         ConsoleConfig (templates/static first) + ElviraAdminConfig (default_site)
  views/          modules (switch page), grid, graph, vectors, jobs — pages + JSON endpoints behind admin_view
  templates/      admin/base_site.html (chrome), console/*.html (index, module page, feature pages)
  static/console/ css/elvira-admin.css (theme), js/*, vendor/* (pinned), fonts/* (Literata, IBM Plex, OFL)
```

Each app keeps its admins in its own `admin.py`, registered with `@register(Model, module="…")`. The site
groups the sidebar and the index by module; a module page lives at `console/module/<key>/`.

**Module gating** is per request (`has_module_permission`/`has_view_permission` consult
`modules.is_enabled`), so switching a module on the Modules page takes effect at the next request, without a
restart. A disabled module's URLs answer 403 (admin views) or 404 (console pages).

**Coverage is enforced**: a test fails when a concrete model is neither registered in a module nor listed in
`EXCLUDED_MODELS` with a reason (today: `Session`, `Permission`, `ContentType`).

### The pages

| Page | What it does |
|---|---|
| Record page (every change view) | Hero (cover or per-name coloured initials, module and type, title, subtitle, badges, actions), key figures linking to filtered lists, fieldsets and inlines as keyboard-navigable tabs (the tab with a validation error opens first), a side panel with recent activity, every related model with its count (derived from reverse relations) and the record's facts, a sticky save bar. Bespoke pages: publication (cover, authors, LCP copies on loan, reservations, files, loans, jobs, links to portal / graph / similar), catalog (publications, covers, categories, feeds, members, latest publications, largest categories, OPDS), person (loans, reservations, catalog access, API keys, set password), loan, harvest source (last run). `ConsoleMixin.record()` |
| Gallery (publications, catalogs, people) | The changelist's filtered, searched, paginated rows as cards (covers, initials); `?view=gallery\|list`, remembered per model in the session. `ConsoleMixin.gallery` / `card()` |
| Overview | "Needs attention" counters (failed jobs, failed/pending encryptions, subject suggestions, harvest conflicts and match suggestions, failed notifications) and one card per module with planner row estimates |
| Modules | Switches with source (deployment default / switched here / feature flag), sensitive badge, reset |
| Data grid (`console/grid/<app>/<model>/`) | Tabulator in remote mode over the admin's own queryset and search: paging, sorting and header filters on whitelisted columns (operators and ranges for numbers and dates), column chooser remembered per model, CSV export (≤ 50 000 rows), estimated counts on big tables; linked from every changelist |
| Graph (`console/graph/`) | Cytoscape explorer: search a publication, category, author or concept, double-click to expand (≤ 60 nodes per step); typed edges from the decider, section similarity, outlines, citations, categories, authors; min-`p` slider, edge-type filters, layouts; built from the relational tables the AGE graph is projected from, so it works without AGE |
| Vectors (`console/vectors/`) | PCA map of entry (or extraction-run) embeddings coloured by catalog/language/category, one embedding model at a time, sampled above 5 000 points, cached by fingerprint; nearest neighbours by cosine distance; per-column coverage (sampled on big tables); passage search through `search_passages` |
| Jobs (`console/jobs/`) | Live (5 s, paused when hidden) KPIs, runs over time by status, runtime per kind, kind × state heatmap, broker queue depths, jobs/runs table with detail drawer (spec, runs, events, traceback, trace link via `EVILFLOWERS_TRACE_URL_TEMPLATE`); retry / cancel / reconcile through the IP-031 facade |

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `EVILFLOWERS_ADMIN_ENABLED` | `1` | `0` = the admin is not mounted (404) |
| `EVILFLOWERS_ADMIN_PATH` | `admin/` | Mount path |
| `EVILFLOWERS_ADMIN_MODULES` | unset (each module's default) | Comma list of module keys, or `all` |
| `EVILFLOWERS_ADMIN_READ_ONLY` | `0` | `1` = no add/change/delete anywhere |
| `EVILFLOWERS_TRACE_URL_TEMPLATE` | empty | Trace link on the Jobs page, `{trace_id}` replaced |
| `CSRF_TRUSTED_ORIGINS` | empty (+ origin of `EVILFLOWERS_BASE_URL`) | Origins allowed to POST the login form |
| `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | `1` in `production.py` | `0` only for a local http run of production settings |

Instance setting `admin.modules` (`{"reading": true, "interop": false}`), changed on the Modules page, via
`PATCH /api/v1/settings/admin.modules` or MCP `update_setting`.

### Deployment

- `Dockerfile` runs `collectstatic` (production settings, no DB, no secrets) at build; WhiteNoise serves
  `/static/` from the web process with hashed names, `immutable` caching and pre-compressed bodies. This works
  unchanged for the Compose prod container, the k8s web Deployment, and a future read-only root filesystem.
- No nginx or Envoy change is needed. The admin login POST must pass Django's origin check behind the
  proxies: `EVILFLOWERS_BASE_URL` (already set on dev and prod) is trusted automatically.
- New dependency `whitenoise` (6.12.0).

## Implementation Plan

### Phase 1 — Static files and theme
- [x] WhiteNoise middleware, `STORAGES` (manifest storage in `production.py`), `collectstatic` in the Dockerfile
- [x] Theme: `--e-*` tokens from elvira.digital mapped onto Django admin variables, light/dark/auto
- [x] Self-hosted fonts (Literata, IBM Plex Sans/Mono; latin + latin-ext), favicon, wordmark
- [x] Test that the manifest build post-processes every static file (it caught a missing Tabulator source map)

### Phase 2 — Whole database in modules
- [x] Module registry, three-layer switch, `admin.modules` setting, Modules page
- [x] `ConsoleModelAdmin`: read-only default, big-table mode, vector exclusion, raw-id FKs, attention counters
- [x] Admins for all 60 project models (+ Group, LogEntry), `UserAdmin` with hashed-password forms
- [x] Readable default `__str__` on `BaseModel`
- [x] Coverage, smoke (every changelist/add/change view), gating, read-only and login tests

### Phase 3 — Interactive pages
- [x] Data grid (Tabulator) + JSON/CSV endpoint
- [x] Knowledge-graph explorer (Cytoscape)
- [x] Vector explorer (ECharts, numpy PCA, pgvector neighbours)
- [x] Jobs dashboard (ECharts, live polling, facade actions)
- [x] Browser check of every page (Playwright, production settings, light and dark)

### Phase 4 — Deploy and follow-ups
- [ ] Deploy to dev ([knowledge#81](https://github.com/EvilFlowersCatalog/knowledge/issues/81)): image rebuild only; verify `/static/admin/css/base.css` 200, login, modules
- [ ] Decide the Review Questions below
- [ ] Breadcrumbs of stock admin pages name the Django app (`Core`); show the module instead
- [ ] Prod release

## Technical Details

- **Big tables** (`big = True`: paragraphs, sentences, links, locators, references, bibitems, job runs and
  events, read events): unfiltered counts come from `pg_class.reltuples`, `show_full_result_count` is off,
  facets never, text search only on exact keys (`=locator_key`).
- **Vectors** (`VectorField`, `HalfVectorField`, `SparseVectorField`, `SearchVectorField`) are deferred in
  every admin queryset and excluded from forms; the change view shows a "Vectors" summary (dimensions, norm).
- **Front end** pages extend `console/page.html` and talk to their JSON endpoints with `Console.api()`
  (session + CSRF header). Library instances (ECharts, Tabulator, Cytoscape) are kept outside Alpine's reactive
  state: a proxied ECharts instance throws on `setOption` (found in the browser check).
- **Theme specificity**: Django 6 underlines content links with `a:not(#header a, …)`, which weighs (1,1,2);
  the theme overrides it with `#container #main a:link`.

## Alternatives Considered

- **django-unfold / Jazzmin**: faster start, but a second design system to bend into ELVIRA's, Tailwind
  build assumptions (Unfold), and a release cycle the catalog would follow. Django's own variables made the
  re-skin a single CSS file.
- **A separate SPA** (portal-style React app over the REST API): the best UX ceiling, but every model would
  need endpoints, serializers and permissions first. The admin gives CRUD, audit log and permissions for free;
  the interactive pages cover where tables are not enough.
- **nginx serving `/static/`**: needs an IaC change per environment and a shared volume between the image and
  nginx; WhiteNoise needs neither.

## Trade-offs and Risks

- Vendored JS (~1.9 MB, ECharts most of it) lives in the repository; updates are manual (README lists versions).
- Read-only by default means some fixes still need REST/MCP. That is deliberate (services keep invariants).
- `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` are now on in production settings. Every deployment is HTTPS at
  the edge; a plain-http internal use of the admin would lose its session.
- The admin login is not rate-limited (IP-026 limits live in `SecuredView`).

## Success Criteria

- `/static/admin/css/base.css` answers 200 on dev and prod; the admin renders styled.
- Every concrete model is reachable or excluded with a reason (test-enforced).
- A module switched off disappears without a restart; sensitive modules are off by default.
- No admin page loads a vector column or renders an unbounded `<select>`.

## Future Considerations

- Module-aware breadcrumbs and per-module permissions for non-superuser librarians (today `is_staff` =
  `is_superuser`).
- LDAP login for the admin (only `ModelBackend` authenticates sessions).
- More attention counters (stale LCP jobs, unclaimed reservations near deadline).
- Saved grid views shared between operators (database-backed, not localStorage).

## References

- Django admin CSS variables: `django/contrib/admin/static/admin/css/base.css`, `dark_mode.css`
- elvira.digital stylesheet (`--e-*` tokens), read 2026-11-17
- IP-031 (jobs facade), #20 (settings store), IP-026 (rate limits), IP-030 (graph), IP-021 (embeddings)

## Review Questions

**Status**: ⏳ Awaiting Answers
**Review Date**: 2026-11-17
**Reviewer**: Claude AI

The following questions must be answered before deployment to prod:

---

### Q1: Which models may the console write? ⚠️ Medium

**Issue**: Editable today: Catalog (title, slug, visibility — no add), Author, Category, EntryReview (+ actions),
User (+ password), Group, UserCatalog, ApiKey (revoke only), AssistantUserPolicy, NotificationBranding,
NotificationContact, HarvestSource (enable/schedule only), Setting (through the registry). Everything else is
read-only; Entry, Acquisition, License, Job and EncryptedContent in particular.

**Context**: Bare forms bypass services. Opening more writes means routing them through services first.

**Question**: Is this the right write surface?

**Options**:
- [ ] **A**: Keep it (recommended); add service-backed actions where operators ask for them
- [x] **B**: Also allow editing Entry metadata through `EntryService` (needs a form wired to the service)
- [ ] **C**: Make the console fully read-only on prod (`EVILFLOWERS_ADMIN_READ_ONLY=1`), writable on dev

**Answer**:
```
2026-11-17 (jdubec): "I want to have ability to add new catalogs for example. Finish CRUDs"
```

**Resolution**:
```
Full CRUD for the library, accounts, reference data, harvest sources, content policies (new versions),
notifications and settings, each with the REST rules (see "Writes that have services"). Machine-written
state stays read-only. POST tests per model in apps/console/tests/test_crud.py.
```

---

### Q2: Sensitive modules on dev? ⚠️ Medium

**Issue**: `reading`, `conversations` and `audit` default off everywhere.

**Context**: Dev holds real STU readers' data (LDAP accounts); assistant debugging needs chat transcripts.

**Question**: Should dev switch them on by default (`EVILFLOWERS_ADMIN_MODULES=all`) or per need through the
Modules page (stored with who switched)?

**Options**:
- [ ] **A**: Per need on the Modules page, both environments (recommended)
- [ ] **B**: `all` on dev, defaults on prod

**Answer**:
```
```

**Resolution**:
```
```

---

### Q3: Should `/admin/` be reachable from the internet? 🔴 Critical

**Issue**: The admin is mounted on the public hostnames; its login has no rate limit (IP-026 covers only
`SecuredView`) and only local passwords work (`ModelBackend`).

**Context**: Superuser accounts are few, but a brute-force target on a public URL is avoidable.

**Question**: How should access be restricted?

**Options**:
- [ ] **A**: Edge allow-list (STU/VPN ranges) for `/admin/` in the nginx/Envoy config — knowledge issue (recommended)
- [ ] **B**: Move it to an unguessable `EVILFLOWERS_ADMIN_PATH` and add a login rate limit in the catalog
- [ ] **C**: Leave public

**Answer**:
```
```

**Resolution**:
```
```

---

### Q4: LDAP superusers in the admin? ℹ️ Low

**Issue**: Admin login uses Django's `ModelBackend`; LDAP accounts have no usable local password.

**Question**: Add the LDAP backend to `AUTHENTICATION_BACKENDS` for session logins, or keep the admin for
local superusers only?

**Options**:
- [ ] **A**: Local superusers only for now (recommended)
- [ ] **B**: Add an LDAP session backend

**Answer**:
```
```

**Resolution**:
```
```

---

### Q5: Passage searches from the console in the disclosure log? ℹ️ Low

**Issue**: The Vectors page searches passages through `search_passages` with the REST surface, so a
superuser's searches are logged as REST disclosures (#175).

**Question**: Keep that, or add a dedicated `admin` surface to `apps.content.access.Surface`?

**Options**:
- [ ] **A**: Keep REST (auditable as is) (recommended)
- [ ] **B**: Add an `admin` surface and policy rule

**Answer**:
```
```

**Resolution**:
```
```

---

## Discussion

The interactive pages were built in parallel on a shared foundation (`ConsolePage`/`ConsoleApi`,
`console/page.html`, `console.js`, the theme) with strict file ownership per page, then checked together in a
browser.

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-11-17 | jdubec | Initial draft with brainstorm, implementation (Phases 1–3) and Review Questions Q1–Q5 |
| 2026-11-17 | jdubec | Q1 answered: full CRUD with the REST rules; grid no longer lists excluded/secret columns (bind credentials, passphrase hashes); read-only switch also removes actions; per-request module memo; big tables order by pk and search by `__exact`; Docker build and image static serving verified; deployment issue knowledge#81. Known unrelated failure: `apps.core.tests.test_tracing…test_response_carries_the_server_spans_traceparent` expects trace flags `-01`, OTel SDK 1.45.2 emits `-03`; fails in isolation, code untouched, not confirmed on a clean checkout |
| 2026-11-17 | jdubec | Redesigned UI for significant models: record pages for every change view (bespoke for publications, catalogs, people, loans, harvest sources), gallery views, per-name initials colours, `--e-on-accent` for readable accent buttons in dark mode; full suite 2252 tests, same single unrelated failure |
