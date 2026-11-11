---
draft: false
date: 2026-11-11
authors:
  - jdubec
categories:
  - Operations
tags:
  - observability
  - opentelemetry
  - tracing
  - tempo
---

# IP-029: Semantic Distributed Tracing Across the Elvira Ecosystem

IP-019 made every catalog process export OpenTelemetry. The traces it produces are correct, but they
only describe the plumbing: a request, the SQL it ran, a Celery hop. This proposal makes them describe
the *library*. Each step is named. The ids a request is about go onto its spans, under the keys the
extraction services already use. Network calls that were invisible get CLIENT spans. Batch work is split
into linked traces. Processes that never traced (`manage.py` commands) now trace too. The result is one
TraceQL query on an entry, acquisition or extraction-run id that returns every span of every service
that touched it, in a hierarchy that reads like the work that was done.

<!-- more -->

## Status

**Status**: Draft
**Last Updated**: 2026-11-11
**Implementation**: Phases 1–3 implemented on `develop`, not committed, tested locally (28 new tests in
`apps/core/tests/test_tracing.py`; the affected apps' suites green) and smoke-checked with the console exporter (see
[Verification](#verification)). Not deployed; nothing has been verified on `evilflowers-dev`. Phase 5
(other repositories) is a list of work, not started.

## Problem Statement

Read in Tempo on 2026-11-11 (dev, the last 7 days):

- **Flat traces.** A catalog request is one SERVER span with every SQL statement and Redis call
  directly under it. `GET api/v1/entries/<uuid:entry_id>/extraction` has 15 children: `SELECT users`,
  `SELECT entries`, `SELECT acquisitions`, six `COUNT(...)`s, five `LLEN`s. Nothing says which of them
  were authentication, which built the response, or which entry the request was about. The id is only in
  `url.path`, unindexed.
- **No domain vocabulary in the catalog.** The extraction services already emit
  `evilflowers.extraction.run_id`, `evilflowers.acquisition_id` and `evilflowers.extraction.job` on
  their spans, and the analyzer has `analyzer.layout` / `analyzer.layout.batch` steps. The catalog side
  of the same pipeline (the `/extraction/v1/runs/<run_id>/…` callbacks) carries none of them, so a run id
  finds the services' spans but not the catalog's. The only hand-made catalog spans are the MCP tool
  calls, the assistant's model calls and the event handler's `celery.event`.
- **Orphaned pipelines.** Many analyzer traces start at `run/evilflowers_analyzer.extract` with no
  parent. Their `celery.hostname` (`gen538@evilflowers-…`, a high PID in an older catalog pod) is
  consistent with `manage.py backfill_extraction` run through `kubectl exec`; it is not proven. What is
  certain: no management command calls `telemetry.setup()`, so the 29 commands produce no spans and
  send no `traceparent`. Request-queued runs are fine: `POST api/v1/entries/<entry_id>/extraction`
  continues into the analyzer, image and text services in one trace, in one case after about 40 hours
  in the queue.
- **Invisible network calls.** LDAP (every Basic login against `ldap.stuba.sk`, and the IP-025 sync),
  SMTP (every notification) and `urllib` (DOI/ISBN introspection) have no instrumentation library, so
  their latency hides inside the parent span and they are missing from Tempo's service graph.
- **Who is calling is unknown.** Service callbacks (analyzer, image, text, lcpencrypt) authenticate with
  API keys, but nothing on the span says which key. A slow callback cannot be attributed to a service
  without reading the user agent.
- **Browser clicks cannot start traces.** The catalog's CORS allow-list is `django-cors-headers`'
  default, so a browser-instrumented portal sending `traceparent` would have every cross-origin request
  refused in preflight.

## Proposed Solution

One module, `evil_flowers_catalog/tracing.py`, holds the vocabulary and the helpers. Call sites use it
and nothing else; `telemetry.py` keeps deciding *whether* a process traces.

### Hierarchy versus links

- **Child span** when the work is the direct continuation of its parent: request → domain step →
  query; request → Celery task → service callbacks. This is what the auto-instrumentation already does
  across processes; this proposal adds the middle layer.
- **Link** for batch fan-out, re-queues and scheduled work on a record an earlier trace created. A
  backfill over 400 PDFs as one trace would hold 400 multi-minute pipelines and could not be opened.
  `tracing.root_span()` gives each item a new trace with a link to the batch span (`evilflowers.link.kind
  = batch`). From an item, Grafana follows the link to the batch; from the batch, the items are a query,
  `{ link:traceID = "<batch trace id>" }`. Not an event per item on the batch span: the SDK keeps at most
  128 events per span, and a backfill has hundreds of items.

### Vocabulary

Ids are attributes, never part of a span name: names stay a closed set, so span metrics keep a bounded
cardinality. Keys the extraction services already emit are spelled exactly as they emit them.

| Attribute | Set from |
|---|---|
| `evilflowers.catalog.id`, `evilflowers.catalog.url_name` | route `catalog_id` / `catalog_name` |
| `evilflowers.entry.id` | route `entry_id`; domain spans |
| `evilflowers.acquisition_id` *(shared with the services)* | route `acquisition_id`; extraction, LCP spans |
| `evilflowers.extraction.run_id`, `.job`, `.region_id` *(shared)* | routes `run_id`, `job`, `region_id`; `extraction.*` spans |
| `evilflowers.readium.license.id`, `evilflowers.readium.reservation.id`, `lcp.content_id` | routes; `lcp.license.*` spans |
| `evilflowers.feed.id`, `.category.id`, `.author.id`, `.review.id`, `.annotation.id`, `.assistant.chat.id`, `.user_acquisition.id` | routes |
| `evilflowers.auth.method` (`none`/`bearer`/`basic`), `evilflowers.auth.token_type` (`access`/`api_key`/`scoped`), `evilflowers.auth.superuser`, `evilflowers.api_key.id` | `SecuredView.dispatch` |
| `enduser.id` | only with `EVILFLOWERS_TRACE_ENDUSER=true` ([Q1](#q1-the-readers-id-on-spans-medium)) |

Routes that address a person (`user_id`, `contact_id`, `api_key_id`, `shelf_record_id`) are left out
on purpose.

### Route ids

`tracing.RouteAttributesMiddleware.process_view` copies the URL keywords above onto the server span.
`telemetry.instrument_django()` appends it to `MIDDLEWARE`, so it exists only when the process traces.
`process_view` runs after URL resolution, inside the OpenTelemetry middleware's span.

### Domain spans

All INTERNAL unless noted. Each is created only inside a trace that is already recording
(`require_parent=True`), so outside a request (a shell, a test) the helpers cost one lookup and
record nothing.

| Span | Where | Attributes |
|---|---|---|
| `auth.authenticate` | `SecuredView._authenticate`, only when a credential was sent | method, token type, outcome |
| `auth.source` | `BasicBackend.authenticate`, per auth source tried | source id, driver, outcome |
| `ldap bind`, `ldap search` (CLIENT) | `ldap_directory.ldap_bind` / `search_profile`, `BasicBackend._ldap` | `server.address`/`port`, bind mode, found; never the DN |
| `ldap.sync.user` | `ldap_directory.sync_users`, per user: a linked root under the `command` role, a child span inside a request ([Q2](#q2-per-user-ldap-sync-traces-when-called-from-a-request-medium)) | auth source id |
| `search` | `SearchState.apply` for the main result (facet passes get no span of their own) | channels, word count, level, hits per channel, results, degraded; never the text |
| `search.lexical`, `search.vector`, `search.embed_query` | `SearchState`, only when a ranking is computed (a facet's memo hit adds nothing) | hits, level, degraded |
| `extraction.queue` → `extraction.dispatch` | `apps.extraction.services` | run id, acquisition id, pipeline version, decision (`queued`/`requeued`/`kept`) |
| (on the callback's server span) | `report_job`, `fail` | job, job failed, run status |
| `lcp.license.issue`/`fetch`/`renew`/`return`/`revoke`/`cancel` | `LicenseService` | licence id, entry id, LCP licence id, state, content id |
| `notification.send` → `smtp send` (CLIENT) | `NotificationService.send` | type, status, log id; relay `server.address` for the SMTP backend only |
| `introspection.resolve` → `GET` / `isbnlib meta` (CLIENT) | `EntryIntrospectionService` | driver, `url.full` through OpenTelemetry's `redact_url` (as for `requests`) |
| `command <name>` (root) | `TracedCommandMixin.run_from_argv` | command name |
| `extraction.backfill.item`, `lcp.encrypt.backfill.item` (root, linked) | `backfill_extraction`, `encrypt_readium_content` | acquisition/entry ids |

**Errors.** A domain span is marked ERROR only for a fault. An exception listed as `expected` (a wrong
LDAP password, a reader with no passphrase, a title already borrowed, no free copy, a DOI the registry
does not know, a fetch/renew/return of a loan in the wrong state, which `LicenseService` raises as
`ValueError`) and any 4xx `ProblemDetailException` set `error.type` and an `exception` event, but leave
the status UNSET, matching IP-019 Q4. `NotLendableError` stays an ERROR: it is the operator's to fix.
A command that exits non-zero (Django turns `CommandError` into `SystemExit`) marks its root span ERROR
with `process.exit.code`.

Pre-existing and out of scope here: `apps/opds2/views/borrow.py` answers *any* exception from
return/renew, a wrong-state `ValueError` included, with a 500, while `/readium/v1/licenses` answers a
ProblemDetail. The server span follows the view, so those 500s stay errors in span metrics.

### Commands

`TracedCommandMixin` overrides `run_from_argv`, the CLI path only. It runs `profiling.setup("command")`,
then `telemetry.setup("command")`, `exit_on_sigterm()`, one root span `command <name>`, and
`telemetry.shutdown()` in `finally`. The `command` role exports traces only: a command gets a new
`service.instance.id` per run, so its process metrics would be a fresh set of Mimir series every time.
`call_command`, which tests and other commands use, goes through `execute` and never touches the
process's telemetry; this avoids IP-019's first-call-wins trap.
`celery_event_handler` keeps its own role.

Applied to the commands that do real work: `backfill_extraction`, `encrypt_readium_content`,
`embed_entries`, `reindex_search`, `reconcile_expired_licenses`, `sync_ldap_profiles`, `load_catalog`,
`purge_catalog`, `regenerate_thumbnails`, `backfill_table_of_contents`, `backfill_cover_hashes`,
`summarise_chapters`, `test_notification`, `storage_audit`, `assistant_eval` and `backup`
([Q5](#q5-which-commands-trace-low)).

### Browser trace context

`CORS_ALLOW_HEADERS` = the `django-cors-headers` defaults + `traceparent`, `tracestate`. That is the
catalog's half of browser tracing; the portal's half is in Phase 5.

### Privacy

No usernames, e-mail addresses, DNs, passwords, search text, titles or chat content go into attributes.
Ids are UUIDs. The reader's own id is opt-in, because together with the entry ids on the same span it is
a reading log ([Q1](#q1-the-readers-id-on-spans-medium)).

## Implementation Plan

### Phase 1: Vocabulary and helpers

- [x] `evil_flowers_catalog/tracing.py`: attribute keys, `span`, `traced`, `root_span`, `client_span`,
      `set_attributes`, `set_route_attributes`, `set_caller`, `RouteAttributesMiddleware`,
      `TracedCommandMixin`
- [x] `telemetry.ROLES` gains `command`; `instrument_django()` appends the route middleware
- [x] `EVILFLOWERS_TRACE_ENDUSER` setting (default off); `CORS_ALLOW_HEADERS` with trace context

### Phase 2: Boundaries

- [x] `TracedCommandMixin` on 16 commands; per-item linked traces in `backfill_extraction` and
      `encrypt_readium_content`
- [x] LDAP CLIENT spans (login and sync); per-user traces in `sync_users` (linked under `command`,
      children inside a request)
- [x] SMTP CLIENT span; `urllib` CLIENT spans in DOI introspection

### Phase 3: Domain spans

- [x] Authentication (`auth.authenticate`, `auth.source`, caller attributes on the server span)
- [x] Search (`search`, per-channel children, query embedding)
- [x] Extraction (`extraction.queue`, `extraction.dispatch`, callback attributes)
- [x] LCP licence operations, notifications, introspection
- [x] `apps/core/tests/test_tracing.py`: hierarchy, links, error semantics, privacy, no-op paths, CORS

### Phase 4: Verify on `evilflowers-dev`

- [ ] Deploy; open the trace of one upload and confirm `auth.authenticate` → … and
      `extraction.queue` → `extraction.dispatch` → `apply_async/evilflowers_analyzer.extract` →
      analyzer → image/equation/text → catalog callbacks with `evilflowers.extraction.run_id` on all
- [ ] `kubectl exec … manage.py backfill_extraction --acquisition <uuid> --force`: one `command
      backfill_extraction` trace, one linked `extraction.backfill.item` trace holding the pipeline
- [ ] One Basic login against `ldap.stuba.sk`: `ldap bind`/`ldap search` spans, and an `ldap` node in
      the service graph; same for the SMTP relay after one notification
- [ ] Span-metric cardinality: `traces_spanmetrics_*` series count before and after (span names are a
      closed set; it should grow by the new names only)
- [ ] Measure the overhead on the k6 baseline (#133 rerun): span count per request rises by 1–4

### Phase 5: The rest of the ecosystem (other repositories)

Work in other repositories, not done here. Each is its own issue once this proposal is accepted.

- [ ] **elvira-portal** (GitHub code search on 2026-11-11 finds no `opentelemetry`, `faro` or
      `traceparent` in the repository): browser tracing (Grafana Faro or OpenTelemetry JS `FetchInstrumentation` with
      `propagateTraceHeaderCorsUrls` for the API origin), so a reader's click is the root of the trace.
      Faro → Alloy's `faro.receiver`; needs a knowledge deployment change ([Q6](#q6-browser-tracing-in-the-portal-medium))
- [ ] **evilflowers-lcpencrypt-worker**: in Tempo as `telemetry.sdk.name=beyla` only. Add the SDK
      (`elvira.stuba.sk/instrumentation: otel-sdk` pod label, per Deploy-a-Service) with spans for
      download → encrypt → upload → result hook, and `lcp.content_id` on each
- [ ] **readium-lcp-server**: no service of that kind in Tempo's service list for the last 7 days
      (read 2026-11-11; it may run under another name). Beyla on its pod at minimum, so the catalog's
      `requests` CLIENT spans to LCP/LSD get a server side
- [ ] **evilflowers-image-service**: rename `figures.kept` / `figures.regions` to
      `evilflowers.figures.*`, matching analyzer and text ([Q4](#q4-one-attribute-namespace-across-services-low))
- [ ] **evilflowers-analyzer/text/equation**: no change needed for linking; confirm they set
      `evilflowers.extraction.run_id` on the CONSUMER span (analyzer and image do)
- [ ] **knowledge**: a "Tracing conventions" note (this vocabulary, the hierarchy/link rule) linked from
      Deploy-a-Service, so new services adopt the same keys

## Technical Details

### Files

| File | Change |
|---|---|
| `evil_flowers_catalog/tracing.py` | new |
| `evil_flowers_catalog/telemetry.py` | `command` role; route middleware |
| `evil_flowers_catalog/settings/base.py` | `EVILFLOWERS_TRACE_ENDUSER`, `CORS_ALLOW_HEADERS` |
| `apps/core/views.py`, `apps/core/auth.py` | auth spans, caller attributes, LDAP binds |
| `apps/core/services/ldap_directory.py` | `ldap_bind`, search span, `_sync_user` + per-user traces |
| `apps/core/search/query.py` | search spans |
| `apps/extraction/services.py` | queue/dispatch spans, callback attributes |
| `apps/readium/services/license_service.py` | licence spans |
| `apps/notifications/services.py` | notification and SMTP spans |
| `apps/api/services/entry_introspection_service.py` | `urllib` CLIENT spans |
| 16 `management/commands/*.py` | `TracedCommandMixin`; `encrypt_readium_content` split into per-entry/per-acquisition methods |
| `apps/core/tests/test_tracing.py` | new, 28 tests |
| `docs/observability.md`, `.env.example` | the span vocabulary, `EVILFLOWERS_TRACE_ENDUSER` |

### Queries this enables (TraceQL)

```text
# Everything any service did for one extraction run
{ span.evilflowers.extraction.run_id = "<uuid>" }

# Every request about one entry, with the steps that ran
{ span.evilflowers.entry.id = "<uuid>" }

# Slow logins, and whether the directory was the slow part
{ name = "auth.authenticate" && duration > 1s } >> { name = "ldap bind" }

# Searches that fell back to semantic only, or ran degraded (TEI down)
{ name = "search" && (span.evilflowers.search.degraded = true || span.evilflowers.search.lexical_hits = 0) }

# Callbacks per service identity
{ span.evilflowers.api_key.id != nil } | by(span.evilflowers.api_key.id)

# A backfill: first its trace, then every item trace linked to it
{ name = "command backfill_extraction" }
{ link:traceID = "<trace id of the command>" }
```

### Testing approach

`tests/test_tracing.py` swaps an in-memory `TracerProvider` in for the global one per test
(`mock.patch.object(trace, "_TRACER_PROVIDER", …)`). `tracing._tracer()` resolves the tracer per span
rather than caching a module-level `ProxyTracer`, which would bind to the first provider it saw and make
that swap impossible.

## Verification

Local only, 2026-11-11, with the real `telemetry.setup()` and `instrument_django()` and the console
exporter (`OTEL_TRACES_EXPORTER=console`, autodiscovery off). Nothing here ran on `evilflowers-dev`.

| Check | Result |
|---|---|
| `manage.py storage_audit --catalog stu` from the CLI | One trace: root `command storage_audit` (`parent_id` null), its 5 `SELECT`s as children, flushed on exit; `OpenTelemetry on: role=command traces=console metrics=none` |
| Same command with a bad argument (argparse exit 2) | Root span status ERROR, `process.exit.code=2`, still flushed |
| Bearer `GET api/v1/catalogs/<catalog_id>/entries/<entry_id>` through the real middleware chain (throwaway test database, search test corpus) | `RouteAttributesMiddleware` last in `MIDDLEWARE`; server span carries `evilflowers.catalog.id`, `evilflowers.entry.id`, `evilflowers.auth.method=bearer`, `token_type=access`, `superuser=true`; child `auth.authenticate` (`outcome=accepted`) → `SELECT users`; the response's 10 queries under the server span |
| Bearer `GET api/v1/entries?query=learning` | `auth.authenticate`; `search` (`lexical_hits=2`, `results=2`, `words=1`, no text) → `search.lexical` → its 2 SQL statements, and `search.vector` (TEI not configured: 0 hits) |
| `GET api/v1/entries` with an invalid bearer token | 401; `auth.authenticate` has `error.type=ProblemDetailException`, status UNSET |

Seen while doing it: a database connection opened *before* `telemetry.setup()` is never instrumented
(the smoke script's test-database creation). gunicorn and Celery set up before their first query, so
production is not affected, but a future entry point must keep that order.

## Alternatives Considered

- **Span per view function / per ORM call via a decorator everywhere.** Doubles the span count with
  spans that repeat the route name. Rejected: spans only at choke points that do I/O or several queries.
- **Ids in span names** (`GET entry 1234`). Unbounded span-metric cardinality in Mimir. Rejected.
- **A blanket `manage.py` hook for commands.** Steals `celery_event_handler`'s role, sets up telemetry in
  `migrate`/`shell`/tests, and decides IP-019's first-call-wins state before tests can. Rejected for an
  explicit mixin on the CLI path.
- **One trace per backfill.** Unopenable at hundreds of items, and Tempo's per-trace limits drop spans.
  Rejected for linked per-item traces.
- **`opentelemetry-instrumentation-urllib`** for introspection. One more dependency and lock change for
  two call sites (and it would not see isbnlib's internal calls any better). Rejected for now.

## Trade-offs and Risks

- **More spans.** One (`auth.authenticate`) per authenticated request; a search adds `search` and one
  span per channel ranking it computes (2–4); an LDAP login adds a bind and a search per directory.
  Phase 4 measures it.
- **Links depend on the viewer.** Grafana shows the link on an item; finding *all* items of a batch is
  the `link:traceID` query, not a click.
- **Per-user LDAP sync traces depend on the process role.** Linked roots under `command`, children
  elsewhere; a Celery task syncing thousands of users would build one large trace
  ([Q2](#q2-per-user-ldap-sync-traces-when-called-from-a-request-medium)).
- **No stored trace context on records.** A licence that expires in the 5-minute sweep cannot yet link
  back to the trace that issued it ([Q3](#q3-links-from-scheduled-work-to-the-trace-that-created-the-record-medium)).

## Success Criteria

- One TraceQL query on an extraction run id returns spans of the catalog *and* every service.
- No analyzer trace on dev without a parent or a link (`{ resource.service.name="analyzer" &&
  nestedSetParent = -1 }` returns nothing after a backfill).
- LDAP and the SMTP relay appear as nodes in the Grafana service graph.
- Span-metric series grow only by the new span names.
- No attribute contains a username, e-mail, DN or query text (asserted in tests).

## Future Considerations

- Store `traceparent` on `License`, `Reservation`, `ExtractionRun` and `EncryptedContent` at creation,
  and link sweep/reminder/promotion spans to it (Q3).
- A `Server-Timing: traceparent;desc=…` response header, so the portal (Faro) and support can quote the
  trace id of a failed request.
- Storage spans (`storage.open`/`save`) once S3 is in use; on the local filesystem they would be noise.
- Tail sampling in Alloy (keep errors and slow traces, sample the rest) once trace volume matters.

## References

- [IP-019: OpenTelemetry Observability](ip-019-opentelemetry-observability.md)
- [IP-018: Intelligent Viewer](ip-018-intelligent-viewer.md) (the extraction pipeline)
- [IP-025: Notification Branding and LDAP E-mail Import](ip-025-notification-branding-ldap-contacts.md)
- knowledge: Deploy-a-Service (telemetry contract), Trace-Encryption-Job
- OpenTelemetry semantic conventions: span links, `enduser.*`, `server.*`, `error.type`

## Review Questions

**Status**: ⏳ Awaiting Answers
**Review Date**: 2026-11-11
**Reviewer**: Claude AI

The following questions must be answered before implementation is considered final. The code follows
the recommended option of each.

---

### Q1: The reader's id on spans ⚠️ Medium

**Issue**: `set_caller` records the credential kind and API key id, and `enduser.id` (the user's UUID)
only when `EVILFLOWERS_TRACE_ENDUSER=true`. The route ids on the same server span include the entry.

**Context**: User id plus entry id in Tempo is a reading log of a university library, retained as long
as Tempo keeps traces and readable by everyone with Grafana access. Loki may already hold the same
pairing in access logs; that has not been checked. Without the id, "why was this reader's borrow slow"
needs the timestamp and the entry instead.

The setting does not make spans anonymous. Whatever it says, the route ids of per-reader records are
recorded next to the entry id: `evilflowers.readium.license.id`, `evilflowers.readium.reservation.id`,
`evilflowers.user_acquisition.id`, `evilflowers.annotation.id`, `evilflowers.assistant.chat.id`. Each
resolves to one reader through the database. They are what makes a loan's trace findable, so they are
on by default; option D removes them.

**Question**: Should spans carry the reader's id by default?

**Options**:
- [ ] **A**: Off by default, opt-in per deployment (dev on, production off) (recommended)
- [ ] **B**: On everywhere; Tempo retention and Grafana access are the control
- [ ] **C**: A keyed hash of the user id (stable per deployment, not reversible without the key)
- [ ] **D**: A, and also drop the per-reader record ids above from `ROUTE_ATTRIBUTES`

**Answer**:
```
```

**Resolution**:
```
```

---

### Q2: Per-user LDAP sync traces when called from a request ⚠️ Medium

**Issue**: A `root_span` per user is right for `manage.py sync_ldap_profiles`. From the MCP tool
`sync_ldap_profiles` (one request) it would move the per-user work out of the request's trace, reachable
only through links. Implemented as option A.

**Context**: The MCP call usually syncs one user or a handful; a child span would read better there.

**Question**: Should per-user traces split off only above a threshold, or only under the `command` role?

**Options**:
- [ ] **A**: Split only when `telemetry.current_role() == "command"`; child spans otherwise (recommended)
- [ ] **B**: Split above N users (e.g. 20) regardless of process
- [ ] **C**: Always split, whatever the process

**Answer**:
```
```

**Resolution**:
```
```

---

### Q3: Links from scheduled work to the trace that created the record ⚠️ Medium

**Issue**: Celery beat sweeps (`expire_lapsed_licenses`, `sweep_unclaimed_reservations`,
`reservation_claim_reminder_sweep`) and re-queues act on records created by earlier traces. Linking to
those needs the creating trace's context stored on the record. None of the models has a field for it.

**Context**: This is the "what happened to this loan over its life" view. It requires a migration on
`License`, `Reservation`, `ExtractionRun` and `EncryptedContent`, and decisions on serializer exposure.

**Question**: Should IP-029 add stored trace context now?

**Options**:
- [ ] **A**: Not in IP-029; a follow-up proposal after Phase 4 shows the rest works (recommended)
- [ ] **B**: Add a nullable `trace_context` column to the four models in IP-029
- [ ] **C**: Store it inside existing JSON fields (`ExtractionRun.provenance`) where one exists, no migration

**Answer**:
```
```

**Resolution**:
```
```

---

### Q4: One attribute namespace across services ℹ️ Low

**Issue**: The services emit `evilflowers.acquisition_id` (underscore) and IP-029 reuses it verbatim, while
its own keys are dotted (`evilflowers.entry.id`). The image service emits unprefixed `figures.kept` /
`figures.regions`.

**Context**: Renaming a shared key breaks saved queries and dashboards in every repository at once;
keeping it leaves one inconsistent key forever.

**Question**: Rename to `evilflowers.acquisition.id` everywhere?

**Options**:
- [ ] **A**: Keep `evilflowers.acquisition_id` as the one exception; fix only the unprefixed `figures.*` (recommended)
- [ ] **B**: Rename across all services in one coordinated change, emitting both keys for one release
- [ ] **C**: Leave everything as is

**Answer**:
```
```

**Resolution**:
```
```

---

### Q5: Which commands trace ℹ️ Low

**Issue**: 16 of 29 commands use `TracedCommandMixin` (option A). Left out: `setup`, `openapi`,
`compile_templates`, `unusablepassword`, `check_production_mode`, `generate_lcp_certification_bundle`,
`check_overshared_licenses`, `dump_catalog`, `entry_defaults`, `dataverse_prepublish_debug`,
`test_events` and `celery_event_handler` (own role).

**Context**: `entry_defaults` runs on every container start (`conf/entrypoint.sh`), which would mean a
trace per pod restart. `assistant_eval` and `backup` are long and do real I/O, which is why A adds them.

**Question**: Is the list right?

**Options**:
- [ ] **A**: The 16 above, including `assistant_eval` and `backup` (recommended, implemented)
- [ ] **B**: Without `assistant_eval` and `backup`
- [ ] **C**: Every command except `celery_event_handler`, `setup`, `openapi`

**Answer**:
```
```

**Resolution**:
```
```

---

### Q6: Browser tracing in the portal ⚠️ Medium

**Issue**: The catalog now admits `traceparent`/`tracestate` in CORS. The portal shows no tracing code
(GitHub code search, 2026-11-11), so it does not send them.
Grafana Faro (RUM + traces) needs a public collector endpoint (`faro.receiver` in Alloy behind nginx),
which exposes an ingestion endpoint to the internet.

**Context**: Without it, a trace starts at nginx; the user's wait before the request (DNS, TLS, the
Angular render) is invisible, and a support report cannot carry a trace id.

**Question**: Which browser instrumentation, and through which endpoint?

**Options**:
- [ ] **A**: Faro Web SDK with tracing, `faro.receiver` behind the edge nginx with a rate limit and an app key (recommended)
- [ ] **B**: OpenTelemetry JS (fetch/XHR instrumentation only) exporting OTLP/HTTP to the same public endpoint
- [ ] **C**: No browser tracing; propagate only a request id from the portal for correlation

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
| 2026-11-11 | jdubec | Initial draft from Tempo findings on dev; Phases 1–3 implemented and tested locally |
| 2026-11-11 | jdubec | Added Review Questions section |
| 2026-11-11 | jdubec | Code follows Q2/Q5 recommendations; batch→item via `link:traceID` instead of events (128-event cap); command role traces-only; facet passes without spans; loan-state `ValueError`s expected; failed commands ERROR; OTel `redact_url` reused; Verification section (console smoke); corrected queue delay and orphan-cause wording, Q1 per-reader ids, Phase 5 evidence |
