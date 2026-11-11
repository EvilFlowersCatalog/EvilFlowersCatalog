# Proposals

Welcome to the proposals archive. This is where all technical decisions, feature designs, and architectural
changes are documented before implementation.

## Proposals Index

**IMPORTANT**: When creating or updating a proposal's status, always update this table to reflect the current state.

| ID | Title | Status | Last Updated |
|----|-------|--------|--------------|
| IP-001 | [Complete LCP Integration & OPDS 2.0 Server with Readium Borrowing](posts/ip-001-lcp-opds2-integration.md) | :white_check_mark: Implemented | 2026-04-08 |
| IP-002 | [Notification Engine with MJML Templates](posts/ip-002-notification-engine.md) | :white_check_mark: Implemented | 2026-07-18 |
| IP-003 | [Readium LCP — EDRLab Certification Readiness](posts/ip-003-lcp-edrlab-certification.md) | :white_check_mark: Implemented (feature scope) · cert/hardening descoped | 2026-07-18 |
| IP-004 | [Configure and Operate Per-Entry Active-License Limits](posts/ip-004-readium-amount-configurability.md) | :white_check_mark: Implemented | 2026-05-14 |
| IP-007 | [Crash-on-First-Contact Bug Triage](posts/ip-007-crash-on-first-contact-triage.md) | :white_check_mark: Implemented | 2026-05-25 |
| IP-008 | [Readium + Dataverse Post-Merge Consolidation](posts/ip-008-readium-correctness-followup.md) | :white_check_mark: Implemented | 2026-05-25 |
| IP-009 | [Readium Integration Closeout — Lifecycle Vocabulary, Expiry Reconcile, and Single-Use Download Tokens](posts/ip-009-readium-integration-closeout.md) | :white_check_mark: Implemented | 2026-05-25 |
| IP-010 | [Multi-Tenancy & ACL Correctness](posts/ip-010-multi-tenancy-acl-correctness.md) | ❌ Rejected (descoped — hardening) | 2026-07-18 |
| IP-011 | [Reservation Queue UX Completion](posts/ip-011-reservation-ux-completion.md) | :white_check_mark: Implemented | 2026-05-26 |
| IP-012 | [AI-Powered Discovery (Search, Recommendations, Summarization, Assistant)](posts/ip-012-ai-powered-discovery.md) | 📝 Draft (review questions resolved) | 2026-07-18 |
| IP-013 | [Document Extraction, Classification & Feature Pipeline](posts/ip-013-document-extraction-structure.md) | 📝 Draft (review questions resolved) | 2026-07-18 |
| IP-014 | [Model Context Protocol Server](posts/ip-014-model-context-protocol-server.md) | 📝 Draft (implemented, awaiting review answers) | 2026-09-10 |
| IP-015 | [Fold `elvira-ai-agent` into the Catalog as `apps/assistant`](posts/ip-015-assistant-fold-in.md) | 📝 Draft (awaiting review answers) | 2026-09-17 |
| IP-016 | [Personal Activity History](posts/ip-016-user-activity-history.md) | ✅ Accepted (implemented, tests not yet run) | 2026-09-21 |
| IP-017 | [Multilingual Feed Titles and Category Labels](posts/ip-017-multilingual-titles.md) | 📝 Draft (implemented, awaiting review answers) | 2026-10-27 |
| IP-018 | [Intelligent Viewer](posts/ip-018-intelligent-viewer.md) | 📝 Draft, Phases 1–6b implemented (catalog `ai-update`, services `main`); not yet run end to end, Phase 7 open | 2026-11-03 |
| IP-019 | [OpenTelemetry Observability for the Elvira LGTM Stack](posts/ip-019-opentelemetry-observability.md) | ✅ Implemented (verified locally; review questions answered; not yet deployed) | 2026-11-06 |
| IP-020 | [Catalog Search Phase 1 — In-Database Full-Text Search, Relevance, Highlights and Facets](posts/ip-020-catalog-search-phase-1.md) | 📝 Draft (implemented and tested locally, awaiting review answers; dev timings and Thorium check open) | 2026-11-09 |
| IP-021 | [Catalog Search Phase 2 — BM25, Semantic Hybrid Search and One Embedding Model](posts/ip-021-catalog-search-phase-2-hybrid.md) | 📝 Draft (catalog implemented and tested with pg_search + real TEI; text-service, cluster and dev measurements open; awaiting review answers) | 2026-11-09 |
| IP-022 | [Assistant Intelligence on a Self-Hosted CPU Model](posts/ip-022-assistant-intelligence.md) | 📝 Draft (Phases 1–3 on develop, dev on `elvira-assistant` since 2026-11-11; Phase 6 — reading publications from a plain chat (#127) and first-evaluation fixes (#130) — tested with a scripted model, real-model re-run open; awaiting review answers Q1–Q11) | 2026-11-11 |
| IP-023 | [Ratings and Reviews for Entries](posts/ip-023-entry-ratings-and-reviews.md) | 📝 Draft (implemented with recommended answers and tested locally; awaiting review answers, portal widgets open) | 2026-11-09 |
| IP-024 | [Grounded Assistant Answers — Evidence Labels, Citation Checks and Robust Structured Output](posts/ip-024-grounded-assistant-answers.md) | 📝 Draft (Phases 1–5 implemented with recommended answers and tested with a scripted model; gold set, support labels and a real-model run open; awaiting review answers) | 2026-11-09 |
| IP-025 | [Notification Branding and LDAP E-mail Import](posts/ip-025-notification-branding-ldap-contacts.md) | 📝 Draft (Phases 1–2 implemented with recommended answers and tested locally; dev backfill and IaC volume open; awaiting review answers) | 2026-11-09 |
| IP-026 | [Request Rate Limits](posts/ip-026-request-rate-limits.md) | 📝 Draft (implemented with recommended answers and tested locally; off by default, enabling is a deployment decision; awaiting review answers) | 2026-11-09 |
| IP-027 | [One HTTP Cache Policy, From Django to the Edge nginx](posts/ip-027-http-cache-policy.md) | 📝 Draft (catalog part implemented and tested locally; nginx part is a knowledge deployment change; #123 itself needs a portal fix; awaiting review answers) | 2026-11-09 |
| IP-028 | [Admin Lending Statistics Endpoint](posts/ip-028-admin-lending-statistics.md) | 📝 Draft (implemented with recommended answers and tested locally; dev check with the portal dashboard open; awaiting review answers) | 2026-11-11 |

**Note on the Readium cluster.** IP-001, IP-003 (feature scope), IP-004, IP-007, IP-008,
IP-009, and IP-011 together constitute the completed Readium LCP delivery and are all
Implemented. IP-002 (notifications) and IP-011 (reservation UX) attach to it. Readium LCP
is considered **done**; remaining certification/deployment items are descoped (see IP-003).

**Note on the AI cluster.** IP-013 (extraction) feeds IP-012 (search, recommendations,
summarization, assistant); neither is started. IP-015 carves the `elvira-ai-agent`
consolidation out of IP-012 Phase 5 so the chatbot can move into the catalog without waiting
on the RAG workstream — see IP-015 Q1 for how the overlap is to be resolved.
IP-018 runs in parallel with IP-012, IP-013 and IP-015: whichever implements a shared piece first
owns it. It deprecates IP-013's "no highlighting" non-goal, and records that none of the
`evilflowers-*-service` containers run in production (2026-10-27). Extraction is not folded into
the catalog: the service repositories are refactored and build their own images, the catalog
compose pulls pinned builds, and text- and search-service are updated rather than retired (which
contradicts IP-012 Phase 1).




**Status Key**:
- 📝 **Draft**: Initial proposal, work in progress
- 🔍 **Under Review**: Proposal complete, awaiting feedback/approval
- ✅ **Accepted**: Approved for implementation
- ✅ **Implemented**: Implementation complete
- ❌ **Rejected**: Proposal declined
- ⏭️ **Superseded**: Replaced by another proposal

## Readium integration reference

- [Integration contract](../readium/integration-contract.md) — endpoint
  catalogue, payload shapes, lifecycle vocabulary, capability-token
  semantics. Published by IP-009 Phase 6.
- [elvira-portal action items](../readium/elvira-portal-action-items.md) —
  frontend punch-list paired with IP-009 (tracked as GitHub issues
  G1–G8 on `EvilFlowersCatalog/elvira-portal`).

## What are Proposals?

Proposals are detailed documents that outline:

- **Problem**: What challenge or need are we addressing?
- **Solution**: Proposed approach to solve the problem
- **Implementation**: Technical details and plan
- **Alternatives**: Other approaches considered
- **Open Questions**: Unresolved issues or decisions needed

## Proposal Lifecycle

1. **Draft**: Initial proposal is written
2. **Under Review**: Team reviews and provides feedback
3. **Accepted**: Proposal is approved for implementation
4. **Implemented**: Solution is built according to proposal

## Writing a Proposal

To write a new proposal:

1. Copy `proposals/.template.md` to `proposals/posts/ip-XXX-title.md`
2. Fill in all sections of the template
3. Add relevant tags and categories
4. **Update this index** with the new proposal entry
5. Submit for review

**Important**: See `CLAUDE.md` for detailed proposal writing guidelines including:
- No time estimates required
- Always update proposal changelog
- Update this index when changing proposal status
