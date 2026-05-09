---
name: proposal-workflow
description: Proposal lifecycle management for monad-knowledge. Use when creating, reviewing, resolving, updating, or implementing proposals (IP-XXX). Covers the 4-step workflow from draft to implementation.
---

# Proposal Workflow

Implementation proposals (IP-XXX) follow a strict 4-step lifecycle. The template at `docs/proposals/.template.md` is the source of truth for proposal structure. The guidelines in `CLAUDE.md` under "Proposal System" define the rules.

## The 4 Steps

| Step | User prompt | What happens |
|------|------------|--------------|
| 1 | "Write/create IP-XXX for ..." | AI drafts full proposal with review questions |
| 2 | "Write/create resolutions for review questions" | AI writes Resolution blocks based on user's Answers |
| 3 | "Update IP-XXX based on resolutions" | AI applies resolutions to proposal body |
| 4 | "Execute implementation of IP-XXX" | AI implements the proposal, marks checklist items done |

---

## Step 1: Create Proposal

**Trigger**: User asks to create/write/draft a proposal.

**Process**:

1. Research the problem — read relevant code, docs, `docs/IDEAS.md` for overlap
2. Copy structure from `docs/proposals/.template.md` (do NOT read it each time — the structure is below)
3. Write the complete proposal with ALL standard sections
4. Read back the created file to verify completeness
5. Review for inconsistencies, contradictions, edge cases, open questions
6. Add Review Questions section with identified issues
7. Update `docs/proposals/index.md` — add row with status `📝 Draft`
8. Update Changelog noting initial draft + review questions

**Proposal structure** (from template):

```
---
draft: true
date: YYYY-MM-DD
authors:
  - jdubec
categories:
  - {Architecture | Feature | Infrastructure | Integration}
tags:
  - tag1
---

# IP-XXX: Title

Brief description (2-3 sentences).

<!-- more -->

## Status
## Problem Statement
## Proposed Solution
  ### Overview
  ### Key Components / subsections
## Implementation Plan
  ### Phase 1–N (checkboxes, no time estimates)
## Technical Details
## Alternatives Considered
## Trade-offs and Risks
## Success Criteria (checkboxes)
## Future Considerations
## References
## Review Questions
## Changelog
```

**Rules**:
- No time estimates or effort calculations anywhere
- Use PlantUML for diagrams (not Mermaid, not ASCII art)
- Check `docs/IDEAS.md` for overlap with existing ideas
- Status section includes: `**Status**: Draft`, `**Last Updated**: YYYY-MM-DD`
- The template shows `**Implementation**: Not started | In Progress | Complete` — include this too

**Review Questions format** — CRITICAL, every question MUST include ALL fields:

```markdown
### Q{N}: {Title} {severity emoji + level}

**Issue**: {description}

**Context**: {why this matters}

**Question**: {specific question}

**Options**:
- [ ] **A**: {option} (recommended if applicable)
- [ ] **B**: {option}
- [ ] **C**: {option}

**Answer**:
\```
[User fills this in — human writes the answer]
\```

**Resolution**:
\```
[AI writes this — describes how proposal will be updated based on the user's answer]
\```

---
```

Severity: `🔴 Critical`, `⚠️ Medium`, `ℹ️ Low`

**What to review for**: schema inconsistencies, contradictions between sections, unhandled edge cases, missing implementation details, ambiguous statements, incomplete migration logic, unresolved dependencies on other proposals, metadata inconsistencies.

---

## Step 2: Write Resolutions

**Trigger**: User asks to "write/create resolutions for review questions" (user has already filled in Answer fields and checked option boxes).

**Process**:

1. Read the proposal, find all review questions
2. For each question where the user filled in `**Answer:**`, write a concrete `**Resolution:**` describing exactly how the proposal body will change
3. Update Review Questions status from `⏳ Awaiting Answers` to `✅ Resolved`
4. Update Changelog

**Rules**:
- Only write resolutions for questions that have answers filled in
- Resolutions must be specific — name exact sections, fields, values, and what changes
- Do NOT modify the proposal body yet — only write Resolution blocks
- If a user's answer is ambiguous, the resolution should interpret it clearly

---

## Step 3: Update Proposal Based on Resolutions

**Trigger**: User asks to "update IP-XXX based on resolutions".

**Process**:

1. Read each Resolution block
2. Apply every described change to the proposal body (sections, diagrams, tables, phases, technical details)
3. Update Changelog

**Rules**:
- Every resolution must produce a visible change in the proposal body
- Update ALL affected locations — don't miss tables, diagrams, or cross-references
- If a resolution says "remove section X", remove it
- If a resolution says "add field Y", add it everywhere Y appears (schemas, examples, migration tables)

---

## Step 4: Execute Implementation

**Trigger**: User asks to "execute/implement IP-XXX".

**Process**:

1. Re-read the proposal (it may have changed since you last saw it)
2. Follow the Implementation Plan phases in order
3. For each completed task, mark `[X]` in the proposal
4. Create necessary files, update existing code, update docs
5. Update `docs/proposals/index.md` status (typically `📝 Draft` → `✅ Accepted`)
6. Update Changelog

**Rules**:
- Read files before modifying them
- Follow project conventions (Poetry, monolithic layout, subcommand pattern)
- Update `.claude/skills/monad-knowledge/SKILL.md` if vault conventions change
- Update `CLAUDE.md` if project-level conventions change
- Items that require CI verification or manual testing should be left unchecked with a note

---

## Proposals Index

`docs/proposals/index.md` tracks all proposals. Update it whenever:
- A new proposal is created (add row)
- A proposal's status changes (update status + date)

**Format**:

```markdown
| ID | Title | Status | Last Updated |
|----|-------|--------|--------------|
| IP-XXX | [Title](posts/IP-XXX-feature-name.md) | {status emoji} {Status} | YYYY-MM-DD |
```

**Status emojis**: `📝 Draft`, `🔍 Under Review`, `✅ Accepted`, `✅ Implemented`, `❌ Rejected`, `⏭️ Superseded`

---

## Post-Implementation

After a proposal is fully implemented:
- Update proposal status to `Implemented`
- Update `docs/proposals/index.md`
- Remove the Review Questions section from the proposal (per template instructions)
- Final Changelog entry