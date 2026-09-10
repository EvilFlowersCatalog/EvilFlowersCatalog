---
draft: true
date: 2026-09-09
authors:
  - jdubec
categories:
  - Feature
  - Integration
tags:
  - mcp
  - model-context-protocol
  - agents
  - llm
  - api
  - json-rpc
---

# IP-014: Model Context Protocol Server

Expose the catalog to LLM agents as a first-class **Model Context Protocol** server: a single
Streamable HTTP endpoint at `/mcp/v1` offering seventeen tools — search and discovery, the
caller's own library, and full management of feeds and categories — plus resources, prompts
and argument completions. Every tool composes the existing `apps.api` filters, so access
control has one implementation, not two. No new runtime dependency.

<!-- more -->

## Status

**Status**: Draft (implemented, awaiting review answers)
**Last Updated**: 2026-09-09
**Implementation**: Complete — `apps/mcp/`, 17 tools, 129 tests

## Problem Statement

The catalog has no MCP surface at all. `.mcp.json` in the repository root configures Claude
Code as an MCP *client* (Playwright, GitHub, the gateway SSH server); nothing in the codebase
makes Elvíra itself something an agent can connect to.

That is now the gap that matters most for AI-facing work:

- **`elvira-ai-agent` re-implements catalog access by hand.** As IP-012 documents, the chatbot
  is a Node/TS service that reaches the catalog only through REST `/api/v1/entries`, duplicates
  the User model, and re-authenticates every turn with a `users/me` round-trip. Every other
  agent that wants to talk to the library will rebuild the same glue.
- **Agents cannot discover the API.** OpenAPI describes the REST surface for humans writing
  code. An agent needs tools with names, argument schemas and *guidance on when to call which*
  — that is precisely what MCP standardises and what OpenAPI does not.
- **The REST payloads are the wrong shape for a context window.** `EntrySerializer.Base` emits
  ~30 fields per entry including the full `config` blob, aliased keys and every null. A page of
  ten entries is largely tokens an LLM cannot use.
- **Getting access control right is easy to get wrong twice.** Any new AI-facing surface that
  queries `Entry.objects` directly re-implements catalog scoping. That is exactly how a
  private catalog leaks.

### What this is *not*

IP-012 explicitly declines to import monad-knowledge's "agentic MCP layer" — its Neo4j
knowledge graph, claim extraction and PageRank research workbench. This proposal is a
different thing that shares a name: a **protocol adapter** in front of the catalog Elvíra
already has. It adds no research machinery, no models, and no infrastructure. The two are
complementary — when IP-012 lands semantic search, `search_entries` gains a retrieval mode
(see Q5) and every already-connected agent benefits without changing its integration.

## Proposed Solution

### Overview

One Django app, `apps/mcp`, serving MCP over Streamable HTTP at `POST /mcp/v1`, authenticated
with the catalog's existing credentials, exposing tools that are thin compositions of
`apps.api` filters, forms and checkers — plus the resource, prompt and completion surfaces the
protocol defines.

```mermaid
graph LR
    A[MCP client<br/>Claude, IDE, agent] -->|JSON-RPC 2.0 over HTTP| B[McpEndpoint<br/>SecuredView]
    B --> C[McpServer<br/>method dispatch]
    C --> D[ToolRegistry<br/>17 tools]
    D --> E[apps.api filters<br/>EntryFilter, CatalogFilter, …]
    E --> F[(PostgreSQL)]
    D --> G[lcp_state_mapping<br/>apps.readium]
    D --> H[projections.py<br/>compact JSON]
    C --> I[resources.py<br/>evilflowers:// URIs]
    C --> J[prompts.py<br/>librarian workflows]
    C --> K[completions.py<br/>live argument values]
    I --> E
    K --> E
    D --> L[apps.api forms<br/>FeedForm, CategoryForm]
    D --> M[checkers<br/>check_catalog_manage]
```

### Key components

1. **`protocol.py`** — JSON-RPC 2.0 envelope and protocol-version negotiation. Knows nothing
   about Django or catalogs.
2. **`registry.py`** — `Tool` (name, title, descriptions, input/output schemas, handler,
   `ToolAccess` level, annotation flags) and the registry that `tools/list` renders and
   `tools/call` dispatches through. The registry filters write tools out entirely when
   `EVILFLOWERS_MCP_ALLOW_WRITE` is off.
3. **`arguments.py`** — coercion and validation of a tool call's arguments. Nothing in the
   stack validates a client's `arguments` against the advertised `inputSchema`, and models
   routinely send a string where an integer belongs; this turns that into a readable message
   rather than a 500.
4. **`projections.py`** — ORM instances into compact JSON: short keys, nulls dropped, prose
   truncated, URLs absolute, everything a JSON primitive.
5. **`schemas.py`** — shared JSON Schema fragments, including the `outputSchema` every tool
   advertises.
6. **`server.py`** — dispatch for `initialize`, `ping`, `tools/*`, `resources/*`, `prompts/*`
   and `completion/complete`; enforcement of each tool's access level; the `instructions`
   string that tells a model how to use the catalog well (and which changes when the write
   surface is off).
7. **`views.py`** — the HTTP endpoint, extending `SecuredView`, plus the three restrictions
   described under *Authentication* below.
8. **`metadata.py`** — the RFC 9728 protected-resource discovery document and the
   `WWW-Authenticate` challenge that points at it.
9. **`resources.py` / `prompts.py` / `completions.py`** — the non-tool protocol surfaces.
10. **`uris.py`** — the `evilflowers://` URI space, shared by resource reads and the
    `resource_link` blocks tool results emit.
11. **`tools/`** — five modules, seventeen tools.

### The tools

Every tool declares a `ToolAccess` level, and the server enforces it before the handler runs —
a new tool cannot forget to authenticate. Object-level permission ("may this user manage *this*
catalog") stays with the handler, because only it knows which object is involved.

| Tool | Purpose | Access |
|------|---------|--------|
| `search_entries` | Free-text + structured search; the primary entry point | public |
| `get_entry` | One publication in full | public |
| `list_catalogs` | Collections this credential may read | public |
| `list_authors` | Resolve an author name to an id | public |
| `list_categories` / `get_category` | Browse the subject vocabulary | public |
| `list_feeds` / `get_feed` | Walk the navigation tree | public |
| `whoami` | Who is this session, what can it read, what can it manage | public |
| `get_my_shelf` | The caller's saved publications | user |
| `list_my_loans` | The caller's Readium LCP licences | user |
| `create_feed` / `update_feed` / `delete_feed` | Curate the navigation tree | **manage** |
| `create_category` / `update_category` / `delete_category` | Curate the subject vocabulary | **manage** |

"public" still means access-controlled — an anonymous session sees public catalogs only. It
describes the *session* requirement, never the data.

`search_entries` accepts `query`, `title`, `author`, `author_ids`, `catalog_id`,
`catalog_title`, `category_ids`, `category_term`, `language_codes`, `published_from`,
`published_to`, `lcp_states`, `readium_only`, `order_by`, `page`, `limit` — the full
`EntryFilter` surface, named for a reader rather than for a query string.

### Design decisions

**Reuse the filters, do not re-query.** Every tool builds a `QueryDict` and hands it to the
`apps.api` filter that already owns that model's access control
(`BaseSecuredFilter.apply_catalog_access_control`). MCP therefore cannot drift from REST on who
may see what. `apps/mcp/tests/test_tools.py::AccessControlTests` is what keeps it that way.

**Relevance ordering is preserved.** `EntryFilter.filter_query` installs a relevance sort;
`search_entries` re-sorts only when the caller asked for a specific `order_by`, or when there
is no `query` at all. This deliberately avoids the bug IP-012 documents on the REST list
endpoint, where `PaginationResponse` rebuilds `order_by` from the `created_at` default and
clobbers relevance.

**Compact projections, not the REST serializers.** Every field in a tool result is spent from a
model's context window. `EVILFLOWERS_MCP_SUMMARY_MAX_CHARS` (600) and
`EVILFLOWERS_MCP_CONTENT_MAX_CHARS` (4000) bound the prose; truncation is flagged in the
payload rather than silent.

**Validate before querying.** Pagination is read at the top of each handler, before the
filter's access control resolves (which runs a query eagerly). A call with `limit=5000` costs
no round-trip.

**Errors go where the model can read them.** Protocol failures — bad envelope, unknown method,
unknown tool — are JSON-RPC errors. Failures *inside* a tool come back as a normal result with
`isError: true` and a message the model can act on ("Unknown argument(s): …. Accepted arguments:
…"). Unexpected exceptions are logged with a stack trace and reported generically.

**Access is declared, enforced centrally, and checked again per object.** Each tool carries a
`ToolAccess` of `PUBLIC`, `USER` or `WRITE`; `McpServer` enforces it before dispatch, so a new
tool cannot forget to authenticate. Object-level permission stays in the handler — it is the
only place that knows which catalog is involved — and uses the same
`has_object_permission("check_catalog_manage", …)` call the REST views use.

**Refusals do not leak existence.** Every management helper resolves the target through the
*read* filter first. A caller who cannot read the record is told it does not exist; only a
caller who can read but not manage it is told they lack permission. Inverting that order would
turn every write tool into an existence oracle for other tenants' catalogs.

**Writes reuse the REST forms.** `create_feed` merges its arguments into a `FeedForm` and
`create_category` into a `CategoryForm`, so validation is literally the same code path as
`POST /api/v1/feeds`. The update tools merge incoming arguments over the record's current
values before validating, which gives a model PATCH semantics without the server growing a
second, laxer validation path.

### Where the management tools are stricter than REST

Three defects surfaced while mirroring the REST endpoints. The MCP tools do not reproduce them;
each is flagged inline in `apps/mcp/tools/feeds.py` and raised as Q8–Q10 below.

1. **`PUT /api/v1/feeds/{id}` permits a cross-catalog move without checking the destination.**
   It calls `check_catalog_manage` on the feed's *current* catalog, then lets the form set any
   `catalog_id`. A manager of catalog A can push a feed into catalog B they do not manage.
   `update_feed` checks both sides.
2. **`(catalog, title)` uniqueness is unchecked.** `Feed.Meta.unique_together` covers both
   `title` and `url_name`; the REST view checks only `url_name`, so a duplicate title reaches
   the database and surfaces as a 500. `create_feed`/`update_feed` check both and return a
   conflict the model can act on.
3. **`Feed.source` is never populated.** `FeedForm` has no `source` field, so `form.populate()`
   leaves the `choices` column at `""`. `create_feed` sets `RELATION`, the only member of
   `Feed.FeedSource`.

### Authentication

The endpoint extends `SecuredView`, so a credential that works against REST works here. Three
restrictions are layered on top, all in `McpEndpoint._authenticate`:

- **Bearer only** (`EVILFLOWERS_MCP_AUTHENTICATION_SCHEMAS`, default `["Bearer"]`). Basic would
  put a reusable password — an LDAP directory password, for LDAP-backed users — into an agent's
  config file. An API key is independently revocable and scoped to one credential.
- **No tokens in the query string.** `SecuredView` accepts `?access_token=`; here it is a 400.
  URLs reach access logs, proxy logs and history. The parent behaviour is untouched for the
  REST and OPDS surfaces that depend on it (notification links, feed readers).
- **Anonymous is optional.** `EVILFLOWERS_MCP_REQUIRE_AUTHENTICATION=1` refuses anonymous
  sessions at the transport, before any JSON-RPC method runs — so a closed deployment does not
  disclose its tool list either.

A 401 carries `WWW-Authenticate: Bearer realm="…", resource_metadata="…"` pointing at an
RFC 9728 document at `/.well-known/oauth-protected-resource/mcp/v1`. It declares the resource
identifier, bearer-in-header, and the deployment's access levels — but deliberately no
`authorization_servers`, because this catalog runs no OAuth authorization server and advertising
a phantom one would send clients into a discovery flow that cannot complete (Q3).

Every management call is written to the `apps.mcp.audit` logger: successes with the affected
object, refusals with the reason.

### Modern protocol surfaces

Beyond tools, the server implements what the current revision offers that a stateless HTTP
deployment can honestly support:

| Surface | What it does here |
|---|---|
| `outputSchema` + `structuredContent` | Every tool advertises its result shape; clients may validate it |
| `resource_link` content blocks | Search hits carry `evilflowers://entry/…` links a client can attach |
| `resources/list` + `templates/list` + `read` | Catalogs enumerated with cursor pagination; entries and feeds by URI template |
| `prompts/list` + `prompts/get` | Four librarian workflows (`reading_list`, `availability_report`, `organise_catalog`, `catalog_overview`) |
| `completion/complete` | Live autocompletion for catalog, category, author and language arguments |
| Tool annotations | `readOnlyHint` / `destructiveHint` / `idempotentHint` so clients prompt before writes |

Resources and completions resolve through the same filters as the tools, so neither can reveal
what a tool would refuse — pinned by `test_protocol_surfaces.py`.

`structuredContent` and `resource_link` are gated on the negotiated revision (both postdate
2025-03-26). What is **not** implemented, and why: `sampling`, `elicitation`, `logging` and
resource `subscribe` all require the server to initiate messages, which needs a persistent SSE
channel this stateless design does not have. Adopting any of them means adopting ASGI and the
official SDK — see Q11.

## Implementation Plan

### Phase 1: Transport — complete

- [x] `protocol.py`: JSON-RPC envelope, error codes, version negotiation over
      `2025-06-18` / `2025-03-26` / `2024-11-05`
- [x] `views.py`: `POST /mcp/v1`, batch support, 202 for notification-only requests,
      405 + `Allow: POST` for `GET`/`DELETE`, `MCP-Protocol-Version` handling, `Origin` guard
- [x] `server.py`: `initialize`, `ping`, `tools/list`, `tools/call`, notifications
- [x] Authentication via `SecuredView`; `WWW-Authenticate` restated on 401

### Phase 2: Tool framework — complete

- [x] `registry.py`: `Tool`, `ToolRegistry`, `@registry.tool` decorator, read-only annotations
- [x] `arguments.py`: closed-set argument validation with model-readable errors
- [x] `pagination.py`: `read_pagination` / `paginate`, clamped rather than 404 on overrun
- [x] `projections.py`: compact entry / catalog / author / category / licence shapes

### Phase 3: Tools — complete

- [x] `search_entries`, `get_entry`
- [x] `list_catalogs`, `list_authors`, `list_categories`
- [x] `whoami`, `get_my_shelf`, `list_my_loans`

### Phase 4: Wiring & documentation — complete

- [x] `apps.mcp` in `INSTALLED_APPS`; route mounted only when `EVILFLOWERS_MCP_ENABLED`
- [x] Six `EVILFLOWERS_MCP_*` settings
- [x] `docs/catalog-wiki/Model-Context-Protocol.md`
- [x] 55 tests: transport conformance, tool behaviour, access control, real Bearer auth

### Phase 5: Feed & category management — complete

- [x] `ToolAccess` (`PUBLIC` / `USER` / `WRITE`) declared per tool, enforced in `McpServer`
- [x] `tools/common.py`: `resolve_catalog_for_management` — read check, then manage check
- [x] `create_feed`, `update_feed`, `delete_feed` reusing `FeedForm` and `FeedFilter`
- [x] `create_category`, `update_category`, `delete_category` reusing `CategoryForm`
- [x] `get_category`, `list_feeds`, `get_feed` to complete the read side
- [x] PATCH semantics (merge over current values, then validate through the all-fields form)
- [x] Destination-catalog permission check on cross-catalog moves (Q8)
- [x] Both halves of `Feed` uniqueness checked (Q9); `Feed.source` populated (Q10)
- [x] `EVILFLOWERS_MCP_ALLOW_WRITE` kill switch — unadvertises rather than refuses
- [x] Audit logging to `apps.mcp.audit`

### Phase 6: Authentication hardening — complete

- [x] Query-string tokens refused (`?access_token=`)
- [x] `EVILFLOWERS_MCP_AUTHENTICATION_SCHEMAS`, defaulting to Bearer only
- [x] `EVILFLOWERS_MCP_REQUIRE_AUTHENTICATION` to close anonymous access
- [x] RFC 9728 protected-resource metadata + `WWW-Authenticate: … resource_metadata="…"`
- [x] `EVILFLOWERS_MCP_MAX_REQUEST_BYTES` body ceiling
- [x] `whoami` reports `manageable_catalogs` and `can_write`

### Phase 7: Modern protocol surfaces — complete

- [x] `outputSchema` on all seventeen tools; `structuredContent` gated on the negotiated revision
- [x] `resource_link` content blocks in tool results
- [x] `resources/list` (cursor-paginated), `resources/templates/list`, `resources/read`
- [x] `evilflowers://` URI space shared by links and reads
- [x] `prompts/list` / `prompts/get` — four librarian workflows
- [x] `completion/complete` for catalog, category, author and language arguments
- [x] Tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`)

### Phase 8: Follow-ups — not started

- [ ] Resolve the review questions below
- [ ] Rate limiting for the anonymous path (Q4)
- [ ] Decide whether Readium write tools (borrow / reserve / shelve) follow (Q2)
- [ ] Decide whether to adopt ASGI + the SDK for sampling/elicitation (Q11)

## Technical Details

### Why no MCP SDK

The official Python SDK builds on Starlette/ASGI. This project is deployed WSGI (gunicorn +
gevent, see `Dockerfile`). A stateless Streamable HTTP server needs no SSE, no session store,
and no async: it is JSON-RPC over `POST`, which is a plain Django view. Adding an ASGI
dependency to serve ~200 lines of dispatch would have been the larger change, and the
transport is small enough to test exhaustively (`test_transport.py`).

### Transport conformance

The Streamable HTTP transport lets a server answer a `POST` with either an SSE stream or a
single JSON body, and lets it omit sessions entirely. This server always answers JSON and
issues no session id, so:

- `GET /mcp/v1` (the SSE-stream opener) → `405` + `Allow: POST`
- `DELETE /mcp/v1` (session teardown) → `405`
- Notifications → `202 Accepted`, empty body
- `MCP-Protocol-Version` absent → treated as `2025-03-26`; unsupported → `400`, except on
  `initialize`, which *is* the negotiation

`structuredContent` is emitted on every `tools/call` result alongside the JSON text block.
Older revisions ignore the unknown key, so one result shape serves all three (Q6).

### Configuration

```bash
EVILFLOWERS_MCP_ENABLED=1              # 0 unmounts the route entirely (404, not a disabled endpoint)
EVILFLOWERS_MCP_DEFAULT_LIMIT=10
EVILFLOWERS_MCP_MAX_LIMIT=50           # deliberately below the REST ceiling
EVILFLOWERS_MCP_SUMMARY_MAX_CHARS=600
EVILFLOWERS_MCP_CONTENT_MAX_CHARS=4000
EVILFLOWERS_MCP_ALLOWED_ORIGINS=       # unset = no Origin check (non-browser clients send none)
```

### Client configuration

```json
{
  "mcpServers": {
    "elvira": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://elvira.stuba.sk/mcp/v1",
               "--header", "Authorization: Bearer ${ELVIRA_API_KEY}"]
    }
  }
}
```

Clients with native Streamable HTTP support connect to the URL directly.

## Alternatives Considered

### Alternative 1: A standalone MCP server wrapping the REST API

**Description**: A separate process (Python or Node) that calls `/api/v1/*` over HTTP.

**Pros**: Zero change to the catalog; independent deploy cadence.

**Cons**: A second service to run, monitor and authenticate; every call becomes two network
hops; access control gets re-derived from REST responses rather than enforced at the query;
it would repeat exactly the `elvira-ai-agent` mistake IP-012 is unwinding.

**Why not chosen**: The catalog already owns the data, the filters and the ACL. Putting the
protocol adapter anywhere else means re-deriving all three.

### Alternative 2: Use the official `mcp` Python SDK

**Pros**: Spec conformance maintained upstream; SSE and sessions for free.

**Cons**: ASGI-oriented, against a WSGI deployment; a new dependency and its transitive tree
for a feature whose whole transport is ~200 lines; the SDK's decorators want to own routing.

**Why not chosen**: See "Why no MCP SDK". Worth revisiting if server-initiated messages
(sampling, elicitation, progress) are ever needed — those genuinely need the SDK.

### Alternative 3: Expose the OpenAPI spec and let clients generate tools

**Pros**: Nothing new to build.

**Cons**: 40+ endpoints with no guidance on which to call; REST payload shapes; no way to hide
write endpoints; no per-tool descriptions.

**Why not chosen**: Tool *curation* is most of the value. Eight well-described tools beat forty
mechanically-generated ones.

## Trade-offs and Risks

### Trade-offs

- **Hand-rolled transport vs. SDK**: we own conformance. Mitigated by `test_transport.py`, and
  bounded because the surface is small and versioned.
- **Compact projections vs. REST parity**: the two surfaces now describe an entry differently
  on purpose. A field added to `EntrySerializer` does not appear in MCP until someone adds it
  to `projections.py`.
- **Read-only**: an agent can find a book and report that it is available, but the user must
  borrow it themselves. Deliberate for v1 (Q2).

### Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| A future tool queries models directly and bypasses catalog ACL | High | `AccessControlTests` covers every tool; the filter-composition rule is documented at the top of each tool module |
| Unauthenticated search becomes a scraping or DoS vector | Medium | Endpoint is opt-out via `EVILFLOWERS_MCP_ENABLED`; anonymous callers see public catalogs only; `limit` capped at 50. Rate limiting is unresolved (Q4) |
| `lcp_states` filtering is a Python post-filter, linear in catalog size | Medium | ~1000 books at STU makes this cheap today; it inherits the existing `EntryFilter` behaviour rather than adding a new problem (Q7) |
| Protocol revision churn breaks clients | Low | Three revisions negotiated; adding a fourth is one tuple entry |
| Prompt injection via catalog content reaching a connected agent | Medium | Content is truncated and clearly framed as data; agents remain responsible for their own handling. Worth stating in operator docs |

## Success Criteria

- [x] An MCP client can `initialize`, list tools and call them over `POST /mcp/v1`
- [x] Anonymous callers see public catalogs only; a credential widens that to exactly its grants
- [x] Every id `search_entries` returns is an id `get_entry` can open, for every caller class
- [x] Malformed arguments produce a readable tool error, never a 500
- [x] A librarian with `manage` on a catalog can create, rename and delete its feeds and
      categories through an agent; a reader on the same catalog cannot
- [x] A refusal never discloses whether a record the caller cannot read exists
- [x] Resources and completions cannot surface anything the equivalent tool would refuse
- [x] Turning off `EVILFLOWERS_MCP_ALLOW_WRITE` removes the management tools from `tools/list`
- [x] A 401 tells a client how to authenticate (RFC 9728 discovery)
- [x] No new runtime dependency; `black --check` clean; `manage.py test apps.mcp` green
      (129 tests)
- [ ] A real agent answers a librarian's question end-to-end against the STU corpus
- [ ] A real agent completes a curation task (create a feed, file entries into it) under human
      approval, against the STU corpus

## Future Considerations

- **Write tools** — `add_to_shelf`, `reserve_entry`, `borrow_entry`, annotated
  `destructiveHint` so clients prompt for confirmation.
- **Semantic search** — when IP-012 lands, `search_entries` gains a `mode` argument
  (`keyword` / `semantic` / `hybrid`); connected agents benefit with no integration change.
- **MCP resources** — cover images and acquisition files as resources rather than URLs, once
  clients handle binary resources consistently.
- **Prompts** — server-supplied prompt templates ("find me course reading on X").
- **OAuth 2.1** — protected-resource metadata, if MCP clients standardise on it (Q3).
- **Per-tool metrics** — call counts and latency into the existing Logfire/OTEL instrumentation.

## References

- [Model Context Protocol specification](https://modelcontextprotocol.io/specification)
- [MCP Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [JSON-RPC 2.0](https://www.jsonrpc.org/specification)
- [IP-012: AI-Powered Discovery](ip-012-ai-powered-discovery.md) — the semantic-search work this
  will front
- [IP-013: Document Extraction](ip-013-document-extraction-structure.md)
- `docs/catalog-wiki/Model-Context-Protocol.md` — operator and client guide
- `docs/catalog-wiki/Security.md` — the authentication this endpoint reuses

## Review Questions

**Status**: ⏳ Awaiting Answers
**Review Date**: 2026-09-09
**Reviewer**: Claude AI

The following questions should be answered before this proposal moves past Draft. The
implementation is complete and every question below records the decision that was taken so the
code runs; each can be revised.

---

### Q1: 🔴 Critical — which access-control rule should `get_entry` use?

**Issue**: The two obvious candidates disagree, and the disagreement is pre-existing in REST.

- `EntryFilter` (`apps/api/filters/base.py:30`) grants **anonymous** callers every entry in a
  **public** catalog. This is what `GET /api/v1/entries` uses.
- `EntryChecker.check_entry_read` (`apps/core/checkers.py:56`) returns `False` for any
  unauthenticated user, public catalog or not, and otherwise requires catalog membership. This
  is what `GET /api/v1/catalogs/{id}/entries/{id}` uses.

So on the REST API today, an anonymous client can *list* an entry in a public catalog and then
gets **403** fetching that same entry's detail.

**Context**: MCP makes the inconsistency actively harmful: a model calls `search_entries`, gets
an id, calls `get_entry`, and is refused. It has no way to tell a permissions boundary from a
bug, so it retries, then hallucinates around the gap.

**Question**: Should `get_entry` mirror the *filter* (permissive: public catalogs readable
anonymously) or the *checker* (strict: authentication always required)? And separately — is the
REST inconsistency intended, or a latent bug worth its own issue?

**Options**:

- [ ] **A**: `get_entry` uses the `EntryFilter` ACL — search and detail agree; public catalogs
      are genuinely public. **(implemented)**
- [ ] **B**: `get_entry` uses `check_entry_read` — stricter, but then `search_entries` must
      apply the same rule or the two disagree, which would make MCP anonymous search return
      nothing at all.
- [ ] **C**: A, plus file a separate issue to align the REST detail endpoint with its list
      endpoint.

**Answer**:
```
[User fills this in]
```

**Resolution**:
```
[AI writes this — describes how the proposal and code will be updated based on the answer]
```

---

### Q2: ⚠️ Medium — should v2 add write tools?

**Issue**: Every tool is read-only. An agent can tell a user "this is available now" but cannot
act.

**Context**: The obvious candidates are `add_to_shelf` (harmless, reversible), `reserve_entry`
(consumes a queue slot, capped by `EVILFLOWERS_READIUM_MAX_RESERVATIONS_PER_USER`) and
`borrow_entry` (consumes a licence slot and starts a loan clock). MCP's `destructiveHint`
annotation makes clients prompt before calling, but that prompt is the *client's* choice, not
something the server can force.

**Question**: Which, if any, write tools should exist, and does a loan started by an agent need
an additional server-side confirmation step that the REST API does not require?

**Options**:
- [ ] **A**: Stay read-only. Agents point users at the portal to borrow. **(implemented)**
- [ ] **B**: Add `add_to_shelf` only — reversible, low stakes.
- [ ] **C**: Add shelf + reservation, but never `borrow_entry`.
- [ ] **D**: All three, relying on client-side confirmation.

**Answer**:
```
[User fills this in]
```

**Resolution**:
```
[AI writes this]
```

---

### Q3: ⚠️ Medium — is API-key Bearer auth sufficient, or is OAuth 2.1 needed?

**Issue**: The MCP authorization spec (2025-06-18) describes OAuth 2.1 with protected-resource
metadata discovery. This endpoint authenticates with the catalog's existing static API-key JWTs
instead.

**Context**: For STU, credentials come from LDAP and API keys are issued through the portal, so
OAuth adds a flow nobody needs. But a client that only implements the OAuth path will fail to
connect, and the 401 currently carries a plain `WWW-Authenticate: Bearer realm="…"` rather than
a `resource_metadata` pointer.

**Question**: Is static Bearer acceptable for the foreseeable deployment, or should
`/.well-known/oauth-protected-resource` be served so spec-strict clients can discover the
scheme?

**Options**:
- [ ] **A**: Static Bearer only; document it. **(implemented)**
- [ ] **B**: Static Bearer plus a protected-resource metadata document advertising it.
- [ ] **C**: Full OAuth 2.1 with dynamic client registration.

**Answer**:
```
[User fills this in]
```

**Resolution**:
```
[AI writes this]
```

---

### Q4: ⚠️ Medium — should the endpoint be rate-limited or network-restricted?

**Issue**: `search_entries` is callable anonymously, and there is no throttling anywhere in the
project.

**Context**: The catalog already has `EVILFLOWERS_ALLOWED_IP_RANGES` for IP-gated entries, and
`EVILFLOWERS_MCP_ENABLED` can unmount the route, but neither is a rate limit. An anonymous
caller can page the whole public corpus at 50 records a request, and the `lcp_states` filter
materialises the queryset in Python before filtering (Q7).

**Question**: For the STU deployment, should `/mcp/v1` be public, restricted at the reverse
proxy, or authenticated-only?

**Options**:
- [ ] **A**: Public and unthrottled, matching `/api/v1/entries` and the OPDS feeds today.
      **(implemented)**
- [ ] **B**: Public but rate-limited at nginx.
- [ ] **C**: Authenticated-only — drop the anonymous path entirely
      (`EVILFLOWERS_MCP_REQUIRE_AUTHENTICATION`).

**Answer**:
```
[User fills this in]
```

**Resolution**:
```
[AI writes this]
```

---

### Q5: ℹ️ Low — how should this compose with IP-012's semantic search?

**Issue**: IP-012 will add semantic and hybrid retrieval. `search_entries` currently maps
straight onto `EntryFilter`, which is keyword-only.

**Context**: IP-012 §"What we take from monad-knowledge" rejects the *research* MCP layer. This
proposal is a protocol adapter, not that — but the relationship should be stated so a future
reader does not read the two as contradictory.

**Question**: When IP-012 lands, should `search_entries` grow a `mode` argument
(`keyword`/`semantic`/`hybrid`), or should semantic search be a separate tool?

**Options**:
- [ ] **A**: `mode` argument on `search_entries` — one tool, one mental model, defaults to
      hybrid once available.
- [ ] **B**: A separate `semantic_search` tool, leaving `search_entries` untouched.

**Answer**:
```
[User fills this in]
```

**Resolution**:
```
[AI writes this]
```

---

### Q6: ℹ️ Low — emit `structuredContent` to pre-2025-06-18 clients?

**Issue**: `structuredContent` is sent on every `tools/call` result, including to clients that
negotiated `2025-03-26` or `2024-11-05`, where the field did not exist.

**Context**: JSON consumers ignore unknown keys, and the server is stateless so branching on
the negotiated revision would mean reading it back from a header on every call. The cost is a
duplicated payload (once as JSON text, once structured) — roughly double the response bytes,
though only the text block enters a model's context on older clients.

**Question**: Accept the duplication, or branch on the negotiated revision?

**Options**:
- [ ] **A**: Always emit both. **(implemented)**
- [ ] **B**: Emit `structuredContent` only when the negotiated revision is ≥ 2025-06-18.

**Answer**:
```
[User fills this in]
```

**Resolution**:
```
[AI writes this]
```

---

### Q7: ⚠️ Medium — the `lcp_states` filter materialises the queryset

**Issue**: `EntryFilter.filter_lcp_state` and `filter_over_saturated` call `list(qs)` and filter
in Python (`apps/api/filters/entries.py:353`), then re-filter by pk. `search_entries` exposes
`lcp_states`, so an MCP caller can trigger it.

**Context**: This is pre-existing behaviour, documented in IP-004 Phase 5 as "intended for
paginated admin views; linear in catalog size". At ~1000 books it is cheap. Exposing it to an
anonymous, agent-driven caller changes the traffic profile rather than the cost per call.

**Question**: Keep `lcp_states` on the MCP tool, or withhold it until the filter is pushed into
SQL?

**Options**:
- [ ] **A**: Keep it — availability is one of the most useful things an agent can report, and
      1000 books is small. **(implemented)**
- [ ] **B**: Keep it but require authentication for that argument specifically.
- [ ] **C**: Drop it from the tool schema until the underlying filter is optimised.

**Answer**:
```
[User fills this in]
```

**Resolution**:
```
[AI writes this]
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-09-09 | jdubec | Initial draft, written alongside a complete implementation in `apps/mcp/`. Phase 1 transport (`protocol.py`, `views.py`, `server.py`), Phase 2 framework (`registry.py`, `arguments.py`, `pagination.py`, `projections.py`), Phase 3 eight read-only tools, Phase 4 wiring + wiki page + 55 tests. Added Review Questions section (Q1–Q7); Q1 records a pre-existing REST inconsistency between `EntryFilter` and `check_entry_read` found while implementing `get_entry`. |
