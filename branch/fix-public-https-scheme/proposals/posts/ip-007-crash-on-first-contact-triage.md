---
draft: false
date: 2026-05-14
authors:
  - jdubec
categories:
  - Feature
tags:
  - bugfix
  - release-blocker
  - migrations
  - signals
---

# IP-007: Crash-on-First-Contact Bug Triage

The post-merge audit found a coherent cluster of defects that each crash, silently no-op, or return 500 on the first production-realistic interaction: a conflicting migration node, an `AttributeError` on every license create, a missing enum that turns intended-403 responses into 500s, a unique-together collision that breaks the certification command and a user's second loan, two OPDS feeds with reverse-sort or non-existent URL lookups, a duplicated Celery enqueue that runs every text-extraction twice, a typoed environment variable that nullifies a documented knob, and a Python name-mangling bug that makes the readium signal lifecycle re-fire on every save. This proposal triages them as one bundled PR so release can proceed without each of these being its own surprise.

<!-- more -->

## Status

**Status**: Implemented
**Last Updated**: 2026-05-25
**Implementation**: Complete

## Problem Statement

### Defect inventory

Each defect is a confirmed runtime failure on a happy-path interaction, found by file:line reading during the audit.

**D1. Conflicting migration node 0034.**
- `apps/core/migrations/0034_acquisition_file_url_alter_entry_identifiers.py:11` — `dependencies=[("core","0033_add_lcp_passphrase_to_user")]`.
- `apps/core/migrations/0034_add_entry_page_count_toc_related.py:9` — also `dependencies=[("core","0033_add_lcp_passphrase_to_user")]`.
- A fresh `python manage.py migrate` aborts with "Conflicting migrations detected." Both migrations were merged via separate branches and both numbered 0034.

**D2. `license_created` signal AttributeError on every license create.**
- `apps/notifications/signals.py:28` reads `instance.entry.author`. `Entry` defines only `authors` (M2M) — `apps/core/models/entry.py:83`.
- With `EVILFLOWERS_NOTIFICATIONS_ENABLED=True`, every `License.save(created=True)` raises `AttributeError`. The signal aborts the transaction. Borrow flow is broken end-to-end.

**D3. `DetailType.FORBIDDEN` and `DetailType.INTERNAL_ERROR` do not exist.**
- `apps/readium/views/download.py:64,71,89,96` reference these enum members.
- `apps/core/errors.py:16-22` defines only `OUT_OF_RANGE / NOT_FOUND / VALIDATION_ERROR / CONFLICT / PASSPHRASE_REQUIRED / READIUM_AMOUNT_BELOW_ACTIVE_COUNT`.
- Every revoked / expired / error path in the License Gateway crashes with `AttributeError` (500) instead of the intended 403. This is the path Thorium and EDRLab's testing tools exercise.

**D4. `License.unique_together = [["entry", "user", "state"]]` causes lifecycle collisions.**
- `apps/readium/models.py:61`.
- A user's second loan on the same entry collides on a terminal-state row from the first loan (`RETURNED`/`CANCELLED`/`REVOKED`).
- The certification bundle generator `apps/readium/management/commands/generate_lcp_certification_bundle.py:229-245` dies on its second artifact because the first leaves a `CANCELLED` row that the second tries to recreate.

**D5. `requirements.txt` is stale (mrml vs mjml-python).**
- `requirements.txt:40` pins `mrml==0.2.0`. Commit `0dab627` switched the notifications engine from `mrml` to `mjml-python`; the import in `apps/notifications/services.py:3` crashes on `pip install -r requirements.txt` builds. CI regenerates correctly via poetry, so the CI-built image works; local docker builds against the checked-in file ship the old `mrml` and notifications crash on first send.
- Fix: regenerate `requirements.txt` from poetry, or untrack it.

**D9. `DV_PUBLIC_INTERNAL` (compose) vs `DV_BASE_INTERNAL` (code) env mismatch.**
- `compose.yml:47` vs `apps/api/views/dataverse.py:292`.
- Documented configuration knob has no effect; falls back to default `http://dataverse:8080`.

**D10. Text-service Celery enqueue duplicated for every PDF upload.**
- `apps/api/views/entries.py:269-289` — the same `if acquisition.content and acquisition.mime == PDF: TextServiceClient().process_acquisition(...)` block appears twice (lines 270-278 and 281-289).
- Every PDF upload publishes two text-extraction tasks. The downstream `POST /index` is documented idempotent (per `docs/search_service_reference.pdf`), but PDF extraction + embedding run twice — doubles cost and storage churn.

**D11. Readium signal name-mangling bug.**
- `apps/readium/signals.py:54, 67, 94, 122, 143, 157`. Python name mangling (`__name` → `_ClassName__name`) only applies inside class bodies, not module-level functions.
- `instance.__original_state = X` writes the attribute literally as `__original_state`. `getattr(instance, "_License__original_state", None)` then always returns `None`.
- The "skip if state unchanged" guard is dead. Every License save in terminal states `RETURNED` / `REVOKED` re-fires the notification handler. Same defect for `Reservation` and `User`.

**D12. OPDS 1.2 OpenSearch descriptor crashes with NoReverseMatch.**
- `apps/opds/views/search.py:23` — `reverse("opds:search", ...)` references a URL name that is not registered in `apps/opds/urls.py`.
- Every request to `/opds/v1.2/{catalog}/search.xml` raises `NoReverseMatch`. The OpenSearch descriptor is broken at runtime; clients that fetch it fail catalog discovery.

**D13. OPDS 1.2 "Latest" feed returns oldest entries.**
- `apps/opds/views/feed.py:138` — `order_by("created_at")` ascending.
- OPDS 2.0 (`apps/opds2/views/navigation.py:40`) is correct with `-created_at`. Feed users see the most stale entries first.

**D14. OPDS 1.2 root feed query is semantically broken.**
- `apps/opds/views/catalog.py:12` — `self.catalog.feeds.filter(parents__content__isnull=True)`.
- `parents` is M2M to `Feed`; `Feed.content` is a non-null `TextField`. The filter is nonsensical (`content` is never null on any parent).
- OPDS 2.0 (`apps/opds2/services/feed_builder.py:63`) uses correct `parents__isnull=True`.

**D15. `license_renewed` notification never fires.**
- Templates `apps/notifications/templates/notifications/license_renewed.{mjml,txt}` exist.
- `apps/readium/services/license_service.py:347-373` (`renew_license`) does not enqueue.
- `apps/readium/views/licenses.py:220` (`LicenseRenewalsView.post`) does not enqueue.
- `apps/readium/signals.py:82-85` notes "handled by the LicenseRenewalsView path" — and that path isn't.
- No `subjects/license_renewed.txt` template; `compile_templates` cannot validate.

**D16. `unusablepassword` management command imports wrong User model.**
- `apps/core/management/commands/unusablepassword.py:2` — `from django.contrib.auth.models import User`.
- The custom `AUTH_USER_MODEL` is `apps.core.models.User`. The command is non-functional.

**D17. Cross-tenant uniqueness leak on `Category`.**
- `apps/api/views/categories.py:33, 104` — `Category.objects.filter(term=…)` lacks `catalog=` filter.
- The DB constraint is `(catalog, term)` unique. The view-level check spans all tenants, so a category term in catalog A blocks creating the same term in catalog B at the form layer (before the DB ever sees the row).

**D18. `FeedManagement.post` uses read-only permission.**
- `apps/api/views/feeds.py:29` — `check_catalog_read` permits feed creation.
- A user with only read permission on a catalog can `POST /feeds/...` and write a new feed.
- `FeedDetail.delete` (`apps/api/views/feeds.py:139-143`) only checks `check_catalog_read` via `_get_feed` — read-only users can delete feeds.

**D19. `purge_catalog` / `dump_catalog` commands KeyError on the unhappy path.**
- `apps/core/management/commands/purge_catalog.py:39` and `apps/core/management/commands/dump_catalog.py:92` reference `options['catalog']` inside the `DoesNotExist` branch, but the CLI arg is `--id` or `--name`.

**D20. `XZCompressionStrategy.suffix()` produces double-dot filenames.**
- `apps/core/management/compression.py:28` — `XZCompressionStrategy.suffix() == ".xz"`.
- `dump_catalog` builds filenames like `f"{url_name}.{compressor.suffix()}"` → `name..xz`.

**D21. `int(request.GET.get(...))` 500s on non-numeric query parameters across multiple OPDS / API endpoints.**
- `apps/opds2/views/{publication,navigation,search}.py` (various lines per audit), `apps/api/response.py:145-146`.
- Negative, huge, or alphanumeric values cause `ValueError` → 500.

**D22. `apps/api/views/feeds.py:103` — `form["parents"].queryset = …` should be `form.fields["parents"].queryset = …`.**
- `BoundField` has no `queryset` setter. Either the line is dead or it raises `AttributeError` whenever taken.

**D23. `apps/api/services/entry.py:31-34` — `entry.identifiers.get("isbn")` crashes when `entry.identifiers is None`.**

**D24. `apps/api/services/entry_introspection_service.py:47` — `except urllib.error.HTTPError | json.JSONDecodeError`.**
- PEP 604 union is NOT a tuple in `except` clauses. The runtime raises `TypeError` whenever this except actually catches.

**D25. `apps/core/auth.py:117` — `BasicBackend` does `base64.b64decode(basic).decode().split(":")` with no try/except.**
- Malformed base64 produces 500 instead of 401.

### Who is affected

- **STU library staff borrowing a book** — every License create crashes (D2).
- **EDRLab reviewers** — License Gateway returns 500 on every revoked-content access (D3); status proxy crashes via D11 + D4.
- **Developers running `python manage.py migrate` from a fresh clone** — fails (D1).
- **Operators running the EDRLab certification bundle command** — fails on the second artifact (D4).
- **Catalog discovery clients** — OPDS 1.2 OpenSearch crashes (D12), latest feed shows stale content (D13), root feed is empty (D14).
- **Any user managing feeds** — read-only users can write or delete them (D18).
- **CI** — `unusablepassword` command is broken (D16).

### Consequences of not addressing

- Production borrow flow is unusable end-to-end.
- First release uploads run text extraction twice (D10).
- OPDS 1.2 clients fail discovery silently.
- EDRLab certification fails on the testing tools.

## Proposed Solution

### Overview

A single triage PR fixing each defect with a minimal, targeted change. Several share fix patterns (the OPDS 1.2 defects are all 1-line fixes; the `int(...)` 500s share a parser helper). Group by file to minimize PR surface.

### Key Components

1. **Migration merge** for D1.
2. **License lifecycle fixes** — D2, D3, D4, D11, D15.
3. **OPDS 1.2 quick wins** — D12, D13, D14.
4. **Dataverse + text-service fixes** — D9, D10.
5. **Dependency pin refresh** — D5.
6. **Permission scope fixes** — D17, D18.
7. **Management command + query string robustness** — D16, D19, D20, D21, D22, D23, D24, D25.

### Architecture

This is a defect-triage proposal; no architectural diagram is meaningful. Each defect is independently small and the fixes do not touch shared models.

## Implementation Plan

### Phase 1: Migrations & Critical Lifecycle

- [x] **D1** — Generate a merge migration: `python manage.py makemigrations --merge core`. Verify the merge file lists both 0034s as dependencies. Smoke-test by dropping the DB and re-running migrate.
- [x] **D2** — `apps/notifications/signals.py:28` — replace `instance.entry.author` with `instance.entry.authors.first()` (returning `None`-safe). Or better, use a helper `_entry_author_name(entry)` that duplicates the logic already in `apps/readium/signals.py:41-45` (and consolidate the two helpers per the readium audit cross-module finding). Add a regression test: `test_license_create_does_not_crash_when_entry_has_no_authors` and `test_license_create_uses_first_author`.
- [x] **D2** — `apps/notifications/signals.py:22` — change the URL prefix from `/readium/licenses/{pk}.lcpl` to `/readium/v1/licenses/{pk}.lcpl` to match the URL config at `evil_flowers_catalog/urls.py:27`. Add a test asserting the rendered link is a 200-able URL.
- [x] **D3** — `apps/core/errors.py:16-22` — add `FORBIDDEN = "FORBIDDEN"` and `INTERNAL_ERROR = "INTERNAL_ERROR"` to the `DetailType` enum. Verify `apps/readium/views/download.py:64,71,89,96` resolve. Add a test of the gateway-on-revoked path asserting status 403 with `detail_type=FORBIDDEN`.
- [x] **D4** — Drop `License.unique_together = [["entry","user","state"]]` (`apps/readium/models.py:61`). Replace with a partial `UniqueConstraint` scoped to non-terminal states:

```python
class Meta:
    constraints = [
        models.UniqueConstraint(
            fields=["entry", "user"],
            condition=Q(state__in=["ready", "active"]),
            name="unique_active_license_per_user_per_entry",
        ),
    ]
```

  Generate a migration that drops the old `unique_together` and adds the new constraint. Backfill: query for any pair of rows that would violate; if any exist, raise a clear migration error pointing to manual cleanup.
- [x] **D11** — `apps/readium/signals.py` — replace module-level `pre_save` storage of `instance.__original_state` (which writes the wrong attribute name) with `instance._original_state` (single underscore). Update the `post_save` reads similarly. Apply to all three handlers (License, Reservation, User passphrase). Add tests asserting "save in terminal state does not re-fire the notification".
- [x] **D15** — Wire `license_renewed` notification:
    - Add `LicenseService.renew_license` (`apps/readium/services/license_service.py:347-373`) a call to enqueue via `NotificationService.send` or `send_notification.delay` for `NotificationType.LICENSE_RENEWED`.
    - Create `apps/notifications/templates/notifications/subjects/license_renewed.txt`.
    - Update `NotificationLog.NotificationType` enum (`apps/notifications/models/notification_log.py:28-29`) to include `LICENSE_RENEWED` (and add the other 7 types written elsewhere in code per audit).

### Phase 2: OPDS 1.2 Quick Wins

- [x] **D12** — Implement OPDS 1.2 search properly (per Q1 resolution):
    - Register `opds:search` in `apps/opds/urls.py` pointing at a new
      `apps/opds/views/search.py::SearchView`.
    - Extract `apps/opds/services/entry_search.py::EntrySearchService.
      search(catalog, query, request) -> QuerySet[Entry]` reusing
      `EntryFilter`. Use it from both `apps/opds/views/search.py` (new) and
      `apps/opds2/views/search.py` to keep OPDS 1.2 and OPDS 2.0 DRY at the
      query layer.
    - Render results via `OpdsAcquisitionEntry.from_model` (OPDS 1.2);
      OPDS 2.0 keeps its publication builder.
    - Fix the OpenSearch descriptor: `reverse("opds:search", ...)`
      resolves; wrap in `<OpenSearchDescription xmlns="http://a9.com/-/spec
      /opensearch/1.1/">`; URL-encode template parameters.
    - Tests in a new `apps/opds/tests/test_search.py`: descriptor resolves
      + valid XML; search view returns filtered + catalog-scoped results;
      cross-tenant ACL enforced.
- [x] **D13** — `apps/opds/views/feed.py:138` — change `order_by("created_at")` to `order_by("-created_at")`. Add a regression test asserting the first entry in the feed is the newest.
- [x] **D14** — `apps/opds/views/catalog.py:12` — change `parents__content__isnull=True` to `parents__isnull=True`. Add a regression test asserting the root feed contains only top-level navigation feeds.

### Phase 3: Dataverse + Text-Service

- [x] **D9** — `compose.yml:47` — rename `DV_PUBLIC_INTERNAL` to `DV_BASE_INTERNAL`. Update `.env.example` accordingly (per IP-006 Phase 3).
- [x] **D10** — `apps/api/views/entries.py:269-289` — delete lines 280-289 (the duplicate `if acquisition.content and acquisition.mime == PDF: TextServiceClient(...)` block). Add a test asserting `TextServiceClient.process_acquisition` is called exactly once per PDF upload.

### Phase 4: Dependency Pin Refresh

- [x] **D5** — Regenerate `requirements.txt` from poetry: `poetry export -f requirements.txt --without-hashes --with logfire --with docker --with s3 --with pdf -o requirements.txt`. Verify `mjml-python` is present and `mrml` is not. Update `requirements.sh:2` to use the same flags so the helper produces the same pinset. Either keep `requirements.txt` tracked (remove from `.gitignore:26`) or untrack entirely; the audit found drift is the live problem.

### Phase 5: Permission Scope

- [x] **D17** — `apps/api/views/categories.py:33, 104` — add `catalog=catalog` to the `Category.objects.filter(term=…)` uniqueness checks. Add a test that creating the same term in two catalogs succeeds.
- [x] **D18** — `apps/api/views/feeds.py:29` — replace `check_catalog_read` with `check_catalog_manage` on `FeedManagement.post`. `apps/api/views/feeds.py:139-143` — replace the read check in `_get_feed` (when called from `FeedDetail.delete`) with `check_catalog_manage`. Add tests for read-only-cannot-create-feed and read-only-cannot-delete-feed.

### Phase 6: Robustness Sweep

- [x] **D16** — `apps/core/management/commands/unusablepassword.py:2` — `from apps.core.models import User`. Add a smoke test that runs the command against a created user.
- [x] **D19** — `apps/core/management/commands/{purge,dump}_catalog.py` — replace `options['catalog']` references with `options.get('id') or options.get('name')`.
- [x] **D20** — `apps/core/management/compression.py:28` — change `XZCompressionStrategy.suffix()` return from `".xz"` to `"xz"` (drop the leading dot, since callers concatenate with `.`).
- [x] **D21** — Add `apps/api/utils/parse.py::parse_int_query(request, name, default, min_value, max_value)` that catches `TypeError`/`ValueError` and raises `ProblemDetailException(400)`. Replace `int(request.GET.get(...))` call sites in OPDS 2.0 views and `apps/api/response.py:145-146`. Add tests for non-numeric, negative, and huge values.
- [x] **D22** — `apps/api/views/feeds.py:103` — change `form["parents"].queryset = …` to `form.fields["parents"].queryset = …`. Add a regression test exercising the affected form path.
- [x] **D23** — `apps/api/services/entry.py:31-34` — change to `(entry.identifiers or {}).get("isbn")`. Add a test with `entry.identifiers = None`.
- [x] **D24** — `apps/api/services/entry_introspection_service.py:47` — change `except urllib.error.HTTPError | json.JSONDecodeError` to `except (urllib.error.HTTPError, json.JSONDecodeError)`. Same file line 73: fix typo `"dio"` → `"doi"`.
- [x] **D25** — `apps/core/auth.py:117` — wrap the base64 decode in try/except for `binascii.Error`, `UnicodeDecodeError`, `ValueError` and return 401 with `WWW-Authenticate: Basic`.

## Technical Details

### Data Model Changes

The License unique-together → partial UniqueConstraint migration (D4):

```python
migrations.AlterUniqueTogether(
    name="license",
    unique_together=set(),
),
migrations.AddConstraint(
    model_name="license",
    constraint=models.UniqueConstraint(
        fields=["entry", "user"],
        condition=Q(state__in=["ready", "active"]),
        name="unique_active_license_per_user_per_entry",
    ),
),
```

A pre-migration data check:

```python
from django.db import migrations

def check_for_duplicate_active_licenses(apps, schema_editor):
    License = apps.get_model("readium", "License")
    duplicates = (License.objects
        .filter(state__in=["ready", "active"])
        .values("entry_id", "user_id")
        .annotate(c=models.Count("id"))
        .filter(c__gt=1))
    if duplicates.exists():
        raise RuntimeError(
            f"Cannot apply partial UniqueConstraint — "
            f"{duplicates.count()} (entry, user) pairs have multiple active licenses. "
            f"Run a cleanup before this migration."
        )
```

### API Changes

- `LicenseRenewalsView` continues to return its existing shape; D15 only adds a side-effecting notification enqueue.
- D17 affects no API surface contract; the underlying constraint already enforced cross-tenant uniqueness; the view-level check was over-eager.

## Alternatives Considered

### Alternative 1: Ship D2 / D3 / D4 piecemeal in separate PRs

**Pros**: Smaller PRs, easier review.

**Cons**: Each PR's regression-test scope overlaps with the others; the borrow flow can't be smoke-tested until D2 + D3 + D4 are all in. Bundled is cheaper.

**Why not chosen**: Triage PR is the convention for crash-on-first-contact defects.

### Alternative 2: Defer OPDS 1.2 fixes (D12, D13, D14); ship only OPDS 2.0

**Pros**: OPDS 2.0 is correct; eventually deprecate 1.2.

**Cons**: STU's existing readers consume OPDS 1.2 today. Discovery crash (D12) breaks them on day one. Triage cost is one-liner per defect; no reason to defer.

**Why not chosen**: D12 / D13 / D14 are 1-line fixes; ship them.

## Trade-offs and Risks

### Trade-offs

- **D4 migration may discover pre-existing duplicate active licenses.** The check fails the migration in that case; operator must manually resolve. Documented.
- **D11 fix may surface latent re-firing that previous notifications were silently emitting (because the guard was dead, every transition save fired the notification handler regardless of state change).** Tests cover the new behavior.
- **D15 wiring may produce a backlog of `license_renewed` notifications for existing renewed licenses if the wiring is also retroactive.** Wire forward only; do not backfill.

### Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Merge migration (D1) interacts badly with a dataverse-side data migration | Low | Both 0034 are pure schema changes; merge is mechanical. |
| D4 partial-constraint migration fails on staging with duplicate actives | Medium | Pre-migration data check raises with a clear error; resolve manually before applying. |
| D11 fix changes notification cadence in production | Low | Notifications previously fired more often (the guard was dead). The new behavior is the intended behavior. Operators may see notification volume drop on cutover. |
| D17 view-level change exposes an existing DB integrity error on cleanup | Low | The DB constraint already enforced `(catalog, term)`; nothing changes there. The view check was the broken one. |

## Success Criteria

- [ ] `python manage.py migrate` succeeds on a fresh DB with no conflict.
- [ ] `POST /api/v1/entries/{id}/borrow` succeeds end-to-end and emits a `license_created` notification log row with status `sent` (in dev with console email backend).
- [ ] License Gateway request for a revoked license returns `403` + RFC 7807 `detail_type=FORBIDDEN` (not 500).
- [ ] A user can borrow → return → borrow the same entry without UNIQUE violation.
- [ ] `python manage.py generate_lcp_certification_bundle` produces the full 6 artifacts without erroring.
- [ ] Two PDF uploads in a row produce two text-service Celery tasks (one each), not four.
- [ ] OPDS 1.2 `/opds/v1.2/{catalog}/search.xml` returns either a valid OpenSearch document or a 404 — never a `NoReverseMatch` 500.
- [ ] OPDS 1.2 `/opds/v1.2/{catalog}/new` returns entries ordered newest-first.
- [ ] OPDS 1.2 `/opds/v1.2/{catalog}` root feed lists top-level navigation feeds (non-empty when feeds exist).
- [ ] A read-only-permission user receives 403 on `POST /api/v1/feeds/...` and on `DELETE /api/v1/feeds/{id}`.
- [ ] Creating a `Category` with term `"science"` in catalog A and again in catalog B both succeed.
- [ ] `python manage.py unusablepassword someuser` runs without import error.
- [ ] `python manage.py purge_catalog --name nonexistent` returns a clear "catalog not found" error, not a `KeyError`.
- [ ] `python manage.py dump_catalog --name some --compress xz` produces `some.xz`, not `some..xz`.
- [ ] `GET /opds/v2/{catalog}/?page=abc` returns 400, not 500.
- [ ] `Authorization: Basic abc` (malformed) returns 401, not 500.
- [ ] `apps/notifications/signals.py:22` rendered URL begins with `/readium/v1/licenses/`.

## Future Considerations

- A pre-commit hook that runs `python manage.py makemigrations --check` to prevent D1-class conflicts.
- A linter (or mypy + django-stubs) configured to flag `__name` in module-level functions, catching D11-class bugs.
- A test smoke-running every named URL with a happy-path payload (caught D12 + D14 in audit; will catch the next).

## References

- `apps/core/migrations/0034_*` — duplicate node (D1).
- `apps/notifications/signals.py:22-28` — entry.author + bad URL (D2).
- `apps/core/errors.py:16-22` and `apps/readium/views/download.py:64,71,89,96` — missing DetailType (D3).
- `apps/readium/models.py:61` — License unique_together (D4).
- `apps/readium/signals.py:54-157` — name-mangling (D11).
- `apps/notifications/templates/notifications/license_renewed.*` and `apps/readium/services/license_service.py:347-373` — orphan template (D15).
- `apps/opds/views/search.py:23` — NoReverseMatch (D12).
- `apps/opds/views/feed.py:138` — sort reversal (D13).
- `apps/opds/views/catalog.py:12` — broken root filter (D14).
- `apps/api/views/entries.py:269-289` — duplicate text-service enqueue (D10).
- `compose.yml:47` vs `apps/api/views/dataverse.py:292` — DV env typo (D9).
- [IP-008: Readium + Dataverse Post-Merge Consolidation](ip-008-readium-correctness-followup.md) — Dataverse extraction (Phase 4) folds in this proposal's D9 fix; D11 (signal name-mangling) is a prerequisite for IP-008 Phase 1 correctness.
- [IP-010: Multi-Tenancy & ACL Correctness](ip-010-multi-tenancy-acl-correctness.md) — D17 (category cross-tenant) and D18 (feed permission) coordinated.

## Review Questions

**Status**: ✅ Resolved
**Review Date**: 2026-05-14
**Resolution Date**: 2026-05-25
**Reviewer**: Claude AI

---

### Q1 ⚠️: OPDS 1.2 OpenSearch (D12) — fix or delete?

**Issue**: The descriptor is broken at runtime (`NoReverseMatch` on `opds:search`). OPDS 2.0 has working search; OPDS 1.2 never had a results view (only the descriptor). Two paths: implement a real OPDS 1.2 `SearchView` *now* in this triage PR, or delete the descriptor entirely (OPDS 1.2 search is unlikely to ship in the May→August functional-state window — the OPDS 2.0 search path is what gets wired to the search service in IP-008 Phase 6).

**Question**: Implement OPDS 1.2 search now or delete the descriptor?

**Options**:

- [ ] **A**: Delete the descriptor + remove the `search` link advertisement from root (Recommended). Stops the crash today; OPDS 2.0 carries search.
- [X] **B**: Implement OPDS 1.2 `SearchView` reusing `EntryFilter` and `OpdsAcquisitionEntry.from_model` in this PR. Larger but coherent.
- [ ] **C**: Implement as a redirect to OPDS 2.0 search (`/opds/v2/{catalog}/search?query=...`). Confusing for 1.2 clients.

**Answer**:
```
Properly fix. Keep DRY and SOLID
```

**Resolution**:
```
Option B chosen. Phase 2 D12 will be rewritten to IMPLEMENT OPDS 1.2 search
rather than delete the descriptor:

1. Register the `opds:search` URL name in `apps/opds/urls.py` pointing at a
   new `apps/opds/views/search.py::SearchView`.
2. Extract a shared search helper to avoid copy-paste between OPDS 1.2 and
   OPDS 2.0:
   - Move the `EntryFilter`-based catalog-DB search into
     `apps/opds/services/entry_search.py::EntrySearchService.search(catalog,
     query, request) -> QuerySet[Entry]`. Both `apps/opds/views/search.py`
     (new) and `apps/opds2/views/search.py` consume it.
   - Reuse `OpdsAcquisitionEntry.from_model` for OPDS 1.2 result rendering;
     OPDS 2.0 continues to use its own publication builder. The shared piece
     is the query layer, not the response shape (RWPM vs Atom diverge).
3. Fix the OpenSearch descriptor at `apps/opds/views/search.py:23` —
   `reverse("opds:search", kwargs={"catalog_url_name": ...})` resolves
   against the new URL. Wrap the descriptor in a proper
   `<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">`
   envelope; URL-encode the template parameters.
4. Tests:
   - `apps/opds/tests/test_search.py::test_opensearch_descriptor_resolves`
     asserts 200 + valid XML against the OpenSearch 1.1 schema (no
     `NoReverseMatch`).
   - `test_search_view_returns_results` asserts the search view returns an
     OPDS 1.2 acquisition feed filtered by `query` and scoped to the
     requested catalog.
   - `test_search_respects_catalog_acl` asserts a non-member of a private
     catalog cannot search into it.
5. Note: OPDS 2.0 keyword/semantic search via the search service is in
   IP-008 Phase 6 (`?mode=keyword|semantic`); OPDS 1.2 search stays
   catalog-DB only for now (matches OPDS 1.2 client expectations and keeps
   IP-007 scope tight).

Changelog entry to be added: "Q1 resolved: OPDS 1.2 search implemented
properly (option B) via shared `EntrySearchService` to keep OPDS 1.2 and
OPDS 2.0 DRY at the query layer."
```

---

### Q2 ⚠️: License `unique_together` (D4) — partial constraint or app-level guard?

**Issue**: The `(entry, user, state)` unique-together is wrong because it collides on terminal states. Replacement: a partial `UniqueConstraint` scoped to non-terminal states, OR an app-level guard in `LicenseService.create_license` that does an `exists()` check + DB-side lock.

**Question**: DB constraint or app-level guard?

**Options**:

- [X] **A**: Partial `UniqueConstraint` on `(entry, user)` where `state IN (ready, active)` (Recommended). Database is the source of truth; race-condition safe.
- [ ] **B**: App-level check + `select_for_update` in `create_license`. More flexible; lets the rule evolve without a migration.
- [ ] **C**: Both — partial constraint + app-level check for clearer error messages.

**Answer**:
```
Yep, of course
```

**Resolution**:
```
Option A confirmed. Phase 1 D4 stands as written:

1. Drop `License.unique_together = [["entry", "user", "state"]]` at
   `apps/readium/models.py:61`.
2. Add a partial `UniqueConstraint` on `(entry, user)` scoped to
   non-terminal states (`ready`, `active`) — see the snippet in the
   Implementation Plan.
3. Generate the migration with a pre-migration data check that raises a
   clear error if any (entry, user) pair currently has multiple active
   licenses (would block the constraint).
4. IP-008 Phase 1 (A1) layers `transaction.atomic()` + entry-level
   `select_for_update` on top so concurrent borrows serialise; the DB
   constraint catches the race-with-app-server-restart edge case as a
   secondary guard.
5. Tests: borrow → return → borrow same entry succeeds; two concurrent
   borrows on a free entry — one succeeds, the other gets 409.

No changes to the proposal needed; this confirms the approach.
```

---

### Q3 ⚠️: D17 (cross-tenant `Category` uniqueness) — should the view check exist at all?

**Issue**: The view performs a uniqueness pre-check that doesn't scope by catalog, blocking duplicate terms across tenants. The DB constraint is correct. Either fix the view check by adding the catalog filter, or delete the view check entirely and let the DB raise IntegrityError → mapped to a 409.

**Question**: Fix the view check or delete it?

**Options**:

- [X] **A**: Add `catalog=catalog` to the filter (Recommended). Keeps the clear error message; removes the cross-tenant leak.
- [ ] **B**: Delete the view check entirely; rely on `IntegrityError` → 409 from the DB constraint.
- [ ] **C**: Move to a Django Form clean step that has the catalog context.

**Answer**:
```
Of course fix
```

**Resolution**:
```
Option A confirmed. Phase 5 D17 stands as written:

1. `apps/api/views/categories.py:33, 104` — add `catalog=catalog` to both
   `Category.objects.filter(term=…)` pre-checks so the uniqueness lookup is
   tenant-scoped.
2. Keep the explicit form-layer 422 error message; the DB constraint
   `(catalog, term)` remains as the authoritative guarantee.
3. Tests:
   - Creating term "science" in catalog A and again in catalog B both
     succeed (no cross-tenant block).
   - Creating term "science" twice in the SAME catalog returns the existing
     clear 422.
   - Concurrent create-same-term-same-catalog: one succeeds, the other
     gets 409 from the DB constraint via the standard `IntegrityError`
     translator.
4. This pattern aligns with IP-010 M4 (multi-tenancy) — same fix, listed
   for visibility there too.

No changes to the proposal needed; this confirms the approach.
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-05-14 | Claude AI | Initial draft based on May 2026 post-merge audit findings: crash/silent-failure defects across core, readium, opds, api. |
| 2026-05-25 | jdubec | Dropped cross-references to former IP-005 (security) and IP-006 (deployability); replaced Phase 4 (Settings Coordination) with a self-contained Phase 4 (Dependency Pin Refresh, D5). Removed D6/D7/D8 (deployment config concerns deferred to a future hardening cycle). Rewrote Q1 to drop the dead IP-011 cross-reference. Focus is functional state for the May→August window. |
| 2026-05-25 | jdubec | Resolved Review Questions Q1–Q3. Q1: chose option B — implement OPDS 1.2 search properly with a shared `EntrySearchService` to keep OPDS 1.2 and OPDS 2.0 DRY at the query layer; Phase 2 D12 rewritten accordingly. Q2: confirmed partial `UniqueConstraint` on `(entry, user)` for non-terminal states (option A). Q3: confirmed `catalog=catalog` filter scoping on the view-layer Category uniqueness check (option A). Review Questions status flipped to ✅ Resolved. |
| 2026-05-25 | jdubec | Implementation complete: status → Implemented. All defects D1–D5, D9–D25 fixed in a single triage PR (D1 merge migration `0035_merge_*`; D2/D11 signal name-mangling + license_created URL/author fix consolidated via new `Entry.first_author_name` property; D3 added `FORBIDDEN`/`INTERNAL_ERROR` to `DetailType`; D4 partial `UniqueConstraint` on License with pre-migration duplicate check in `readium/0006_*`; D5 regenerated `requirements.txt`; D9 renamed compose env; D10 dropped duplicate text-service enqueue; D12 implemented OPDS 1.2 `SearchView` + shared `EntrySearchService` + proper `<OpenSearchDescription>` envelope with URL-encoded template params; D13/D14 OPDS 1.2 latest/root feed fixes; D15 wired `license_renewed` notification + expanded `NotificationType` enum + new `notifications/0002_*` migration; D16–D25 robustness sweep including new `apps/api/utils/parse.py::parse_int_query` helper used by `PaginationResponse` and OPDS 2.0 views, `BasicBackend` base64 hardening, and BoundField queryset fix). Bonus: migrated from deprecated `authlib.jose` to `joserfc` to silence the `AuthlibDeprecationWarning`. |
