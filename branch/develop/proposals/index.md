# Proposals

Welcome to the proposals archive. This is where all technical decisions, feature designs, and architectural
changes are documented before implementation.

## Proposals Index

**IMPORTANT**: When creating or updating a proposal's status, always update this table to reflect the current state.

| ID | Title | Status | Last Updated |
|----|-------|--------|--------------|
| IP-001 | [Complete LCP Integration & OPDS 2.0 Server with Readium Borrowing](posts/ip-001-lcp-opds2-integration.md) | :white_check_mark: Implemented | 2026-04-08 |
| IP-002 | [Notification Engine with MJML Templates](posts/ip-002-notification-engine.md) | ✅ Accepted | 2026-04-09 |
| IP-003 | [Readium LCP — EDRLab Certification Readiness](posts/ip-003-lcp-edrlab-certification.md) | ✅ Accepted | 2026-05-12 |
| IP-004 | [Configure and Operate Per-Entry Active-License Limits](posts/ip-004-readium-amount-configurability.md) | :white_check_mark: Implemented | 2026-05-14 |
| IP-007 | [Crash-on-First-Contact Bug Triage](posts/ip-007-crash-on-first-contact-triage.md) | :white_check_mark: Implemented | 2026-05-25 |
| IP-008 | [Readium + Dataverse Post-Merge Consolidation](posts/ip-008-readium-correctness-followup.md) | 📝 Draft | 2026-05-25 |
| IP-010 | [Multi-Tenancy & ACL Correctness](posts/ip-010-multi-tenancy-acl-correctness.md) | 📝 Draft | 2026-05-25 |




**Status Key**:
- 📝 **Draft**: Initial proposal, work in progress
- 🔍 **Under Review**: Proposal complete, awaiting feedback/approval
- ✅ **Accepted**: Approved for implementation
- ✅ **Implemented**: Implementation complete
- ❌ **Rejected**: Proposal declined
- ⏭️ **Superseded**: Replaced by another proposal

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
