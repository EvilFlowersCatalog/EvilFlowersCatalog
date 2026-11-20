---
draft: true
date: 2026-11-09
authors:
  - jdubec
categories:
  - Feature
tags:
  - ratings
  - reviews
  - moderation
  - api
  - opds
---

# IP-023: Ratings and Reviews for Entries

Readers can give a publication a star rating and, optionally, write a review. The catalog stores
both, shows the average and count on the entry, and lets catalog managers hide reviews.

<!-- more -->

## Status

**Status**: Draft
**Implementation**: Implemented with the recommended answer to every Review Question (see each Resolution); the answers still need confirming. Portal widgets open.
**Last Updated**: 2026-11-09
**Issue**: [#57](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/57) (follow-up to #50, deferred by IP-003 Q6)
**Related**: [IP-003](ip-003-lcp-edrlab-certification.md) (Q6, where this was deferred), [IP-016](ip-016-user-activity-history.md) (activity history), [IP-002](ip-002-notification-engine.md) (notifications)

## Problem Statement

The portal's Figma design for the entry detail page (#50) has rating and review widgets. The catalog
has nothing behind them:

- No model, endpoint or serializer field for a rating or a review. `Entry.popularity` counts
  downloads, which says nothing about whether readers liked the book.
- IP-003 delivered the rest of #50 (page count, table of contents, related entries) and deferred
  ratings and reviews. The reason was that nobody had decided where the data lives.
- Search (IP-020, IP-021) and recommendations (`/entries/{id}/similar`) have no signal of quality,
  only text similarity and popularity.

#50 stays open until this lands.

## Proposed Solution

Native Django models in `apps/core`. The catalog owns the data, the permissions and the moderation.
The alternative, a read-only aggregate synced from STU's LMS, is discussed under
[Alternatives](#alternatives-considered). Nothing in this repository talks to an LMS today.

In short:

1. **`EntryReview`**: one row per (user, entry). A required `rating` from 1 to 5, an optional `title`
   and `body`, and a moderation `state`. A rating without text is just a review with an empty body.
2. **Denormalised aggregate on `Entry`**: `rating_count` and `rating_sum`, with the average derived
   from them. They are maintained in the same transaction as the review write, so lists and sorting
   need no join.
3. **Who may review**: any authenticated user who can read the entry (`check_entry_read`). Whether a
   loan or download is also required is Q3.
4. **Moderation**: catalog `manage` users and superusers can hide a review. A hidden review keeps
   its rating in the aggregate or not, see Q2. Authors can edit and delete their own review.
5. **API**: REST endpoints under `/api/v1/entries/{id}/reviews` and `/api/v1/reviews/{id}`, and
   `rating` on the entry serializers. OPDS output is Q6.

### Data model

```python
class EntryReview(BaseModel):
    class State(models.TextChoices):
        PUBLISHED = "published"
        HIDDEN = "hidden"          # by a moderator

    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews")
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    title = models.CharField(max_length=255, blank=True, default="")
    body = models.TextField(blank=True, default="")
    language = models.ForeignKey(Language, null=True, blank=True, on_delete=models.SET_NULL)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PUBLISHED)
    moderated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    moderated_at = models.DateTimeField(null=True, blank=True)
    moderation_note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "entry_reviews"
        constraints = [models.UniqueConstraint(fields=["entry", "user"], name="entry_reviews_entry_user")]
        indexes = [models.Index(fields=["entry", "state", "-created_at"])]
```

On `Entry`:

```python
rating_count = models.PositiveIntegerField(default=0)
rating_sum = models.PositiveIntegerField(default=0)
# rating_avg = rating_sum / rating_count, computed in the serializer and in ORDER BY expressions
```

The aggregate is recomputed for the one entry inside the write transaction
(`UPDATE entries SET rating_count = …, rating_sum = … WHERE id = …` from a subquery over
`entry_reviews`). It is never incremented blindly, so a lost update cannot drift it.
`manage.py recompute_ratings` rebuilds it for all entries.

### API

| Method | Path | Who | Effect |
|---|---|---|---|
| `GET` | `/api/v1/entries/{id}/reviews` | anyone who can read the entry | Paginated published reviews, newest first. `?order_by=rating` / `-rating`. Moderators also see hidden ones with `?state=hidden`. |
| `GET` | `/api/v1/entries/{id}/reviews/mine` | authenticated | The caller's own review, or 404. |
| `PUT` | `/api/v1/entries/{id}/reviews/mine` | authenticated reader | Create or replace the caller's review (declarative, idempotent). |
| `DELETE` | `/api/v1/entries/{id}/reviews/mine` | authenticated | Remove the caller's review. |
| `PATCH` | `/api/v1/reviews/{id}` | catalog `manage`, superuser | `{"state": "hidden", "moderation_note": "…"}` or back to `published`. |

#57 suggested `POST /entries/{id}/reviews` plus `PATCH /reviews/{id}`. One review per (user, entry)
makes `PUT …/mine` the better fit: the client does not need to know whether a review exists, and a
retry cannot create a duplicate (Q4).

Entry serializers (detail and list) gain:

```json
"rating": {"average": 4.3, "count": 12, "mine": 5}
```

`mine` is `null` for an anonymous caller or one without a review. `GET /api/v1/entries` accepts
`order_by=-rating` (best first, like every other column: Bayesian average, then count, then id) and
`rating_min=4`. A `rating` facet with 1–5 buckets is optional (Q7).

### Side effects

- **Activity history (IP-016)**: `ENTRY_REVIEWED` is recorded on create and update.
- **Notifications (IP-002)**: none in this proposal. A "your review was hidden" e-mail is a
  possible follow-up.
- **MCP (IP-014)**: `get_entry` returns the `rating` block. No review-writing tool.
- **Search**: review text is not indexed. Ratings do not change relevance ranking in this proposal.
- **Deletion**: reviews cascade with the entry and with the user (GDPR erasure).

## Implementation Plan

### Phase 1: Model and aggregate

- [x] `EntryReview` model, `Entry.rating_count` / `rating_sum`, migration `core.0048`
- [x] `ReviewService.upsert(user, entry, …)`, `.delete(…)`, `.moderate(…)`, each recomputing the
      entry aggregate in the same transaction; a `post_delete` receiver covers cascades (user erased)
- [x] `Entry.save()` leaves the aggregate columns out, so a stale instance cannot write old numbers back
- [x] `manage.py recompute_ratings` (`--check` reports drift)
- [x] Admin: review list with filters (state, catalog, rating) and hide/publish actions
- [x] Tests: uniqueness, aggregate after create/update/delete/hide, cascade on user delete, stale save

### Phase 2: REST API

- [x] Review endpoints from the table above with forms, serializers and OpenAPI metadata
- [x] Permission checks: the entry-list access rule (public catalog, membership, superuser) for reading
      and writing, catalog `manage` for moderation; an unreadable entry answers 404
- [x] `rating` block on the entry serializers; `order_by=-rating`, `rating_min`
- [x] `ENTRY_REVIEWED` activity action
- [x] Tests: permissions per role, anonymous access, hidden reviews invisible to non-moderators,
      `PUT` idempotence (`apps/core/tests/test_reviews.py`)

### Phase 3: Integrations

- [x] MCP `get_entry` / `search_entries` return `rating` for rated entries
- [x] OPDS output per Q6: none
- [ ] Optional `rating` facet per Q7 (not built, see Q7)
- [x] Settings wiki and CHANGELOG
- [x] Portal issue on `elvira-portal` for the widgets ([elvira-portal#32](https://github.com/EvilFlowersCatalog/elvira-portal/issues/32))

## Technical Details

### Settings

| Env | Default | Description |
|---|---|---|
| `EVILFLOWERS_REVIEWS_ENABLED` | `1` | `0` hides the endpoints and the `rating` block. |
| `EVILFLOWERS_REVIEWS_BODY_MAX_CHARS` | `5000` | Maximum review length. |

A per-catalog switch (some catalogs may not want reviews at all) would live in `Catalog.config`
if Q5 asks for it.

### Average and sorting

Ordering by a raw average puts a single 5-star rating above forty 4.8 ratings. Options are a plain
average with the count as a tie-breaker, or a Bayesian average
`(C·m + rating_sum) / (C + rating_count)` with a catalog-wide prior `m` and weight `C`. Q8.

## Alternatives Considered

### Aggregate synced from STU's LMS

Ratings live in the LMS and the catalog stores only `rating_avg` / `review_count`, refreshed by a
periodic job.

- **For**: no moderation surface in the catalog; reuses whatever STU already has.
- **Against**: no LMS integration exists in this codebase. STU-specific, so other deployments get
  nothing. Reviews could not be shown on the entry page, and a reader could not rate from the
  portal.

### Ratings only, no text

Simpler: no moderation at all. It meets the "stars on the detail page" part of the Figma design
but not the review list. The proposed model allows it as a configuration (`body` disabled, Q1).

### Reuse `Annotation`

Annotations belong to a user acquisition and a page, not to an entry, and are private. Bending
them into public reviews would mix two permission models.

## Trade-offs and Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Abusive or off-topic review text | Medium | Moderator hide; per-catalog switch; Q1 can drop text entirely |
| Rating manipulation by many accounts | Low (accounts come from LDAP/STU) | One review per user; Q3 can require a loan or download |
| Aggregate drift | Low | Recomputed from the table on every write; `recompute_ratings` |
| Privacy: reviews reveal reading habits | Medium | Q9: show reviewer names or not |
| Low participation, empty widgets | Medium | Hide the block when `count = 0` (portal) |

## Success Criteria

- [ ] A reader can rate and review a publication they can read, edit it, and delete it
- [ ] Entry detail and list responses carry `rating.average`, `rating.count` and `rating.mine`
- [ ] A catalog manager can hide a review; hidden reviews are invisible to other users
- [ ] The aggregate equals a recount from `entry_reviews` after any sequence of writes
- [ ] `GET /api/v1/entries?order_by=rating` sorts by the agreed rule
- [ ] #50 and #57 can be closed

## Future Considerations

- Helpfulness votes on reviews
- Report-a-review flow feeding the moderation queue
- Ratings as a signal in recommendations (IP-012) and as a tie-breaker in search ranking
- Notification to the reviewer when a review is hidden

## References

- [Issue #57](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/57)
- [Issue #50](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues/50)
- [IP-003 Q6](ip-003-lcp-edrlab-certification.md)
- `apps/core/models/shelf_record.py`: existing one-row-per-(entry, user) pattern
- `apps/core/models/user_catalog.py`: `read` / `write` / `manage` access levels

## Discussion

None yet.

## Review Questions

**Status**: ⏳ Awaiting Answers (implemented with the recommended options; confirm or change)
**Review Date**: 2026-11-09
**Reviewer**: Claude AI

The following questions must be answered before implementation. Q1, Q2, Q3 and Q4 are the four
questions #57 left open.

---

### Q1: Data ownership: native models or an LMS aggregate? 🔴 Critical

**Issue**: #57 open question 1. The whole proposal assumes native models (Proposed Solution, item 1).

**Context**: An LMS aggregate removes moderation but gives no review list and works only at STU.
No LMS integration exists in this repository.

**Question**: Where do ratings and reviews live?

**Options**:
- [ ] **A**: Native models, ratings and review text (recommended)
- [ ] **B**: Native models, ratings only (no text, no moderation)
- [ ] **C**: Aggregate synced from STU's LMS

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): native models with rating and text.
```

---

### Q2: Moderation: who, and does a hidden review still count? 🔴 Critical

**Issue**: #57 open question 2. The proposal gives moderation to catalog `manage` users and
superusers (Proposed Solution, item 4) but leaves the aggregate rule open.

**Context**: Excluding hidden reviews from the average lets a moderator change a book's score.
Keeping them means an abusive one-star rating stays in the number.

**Question**: Who moderates, and are hidden reviews part of `rating.average`?

**Options**:
- [ ] **A**: Catalog `manage` + superusers; hidden reviews excluded from the aggregate (recommended)
- [ ] **B**: Catalog `manage` + superusers; hidden reviews hide only the text, the rating counts
- [ ] **C**: A new per-catalog moderator role separate from `manage`
- [ ] **D**: Pre-moderation: reviews with text appear only after approval

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): catalog `manage` users and superusers moderate
(PATCH /api/v1/reviews/{id}, admin actions); hidden reviews are left out of rating_count/rating_sum.
An edit by the author does not publish a hidden review again.
```

---

### Q3: Who may review? ⚠️ Medium

**Issue**: Proposed Solution, item 3 allows anyone who can read the entry.

**Context**: Requiring a past loan or download (`UserAcquisition`, `License`) cuts drive-by ratings
but excludes readers of open-access titles read elsewhere, and readers of print copies.

**Question**: Which users may rate an entry?

**Options**:
- [ ] **A**: Any authenticated user who can read the entry (recommended)
- [ ] **B**: Only users with a `UserAcquisition` or a past/current `License` for the entry
- [ ] **C**: A per-catalog setting choosing between A and B

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): any authenticated user who can see the entry in the entry
list (public catalog, catalog membership, superuser). An entry the caller cannot see answers 404.
```

---

### Q4: One review per user, editable; `PUT …/mine` instead of `POST`? ⚠️ Medium

**Issue**: #57 open questions 3 and 4 suggested `POST /entries/{id}/reviews` and
`PATCH /reviews/{id}`. The proposal uses `PUT /entries/{id}/reviews/mine` (API section).

**Context**: With a unique (user, entry) constraint, `POST` either fails the second time or has to
behave like an upsert. `PUT` on the caller's own resource says that directly.

**Question**: Confirm uniqueness per (user, entry), editability, and the endpoint shape.

**Options**:
- [ ] **A**: Unique, editable, `PUT/GET/DELETE …/reviews/mine`, moderation on `PATCH /reviews/{id}` (recommended)
- [ ] **B**: Unique, editable, `POST /entries/{id}/reviews` (409 on a second one) + `PATCH /reviews/{id}`
- [ ] **C**: Several reviews per user (e.g. per edition re-read)

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): unique (entry, user), editable,
PUT/GET/DELETE /api/v1/entries/{id}/reviews/mine (PUT answers 201 new / 200 replaced),
PATCH /api/v1/reviews/{id} for moderation, GET /api/v1/entries/{id}/reviews to list.
```

---

### Q5: Can a catalog switch reviews off? ℹ️ Low

**Issue**: Technical Details mention only a deployment-wide `EVILFLOWERS_REVIEWS_ENABLED`.

**Context**: Some catalogs (e.g. a theses catalog) may not want public reviews.

**Question**: Is a per-catalog switch needed in v1?

**Options**:
- [ ] **A**: Deployment-wide switch only (recommended for v1)
- [ ] **B**: Also `Catalog.config["reviews_enabled"]`

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): EVILFLOWERS_REVIEWS_ENABLED only (default on); off
answers 404 on the review endpoints and returns `rating: null` on entries.
```

---

### Q6: Ratings in OPDS? ℹ️ Low

**Issue**: Phase 3 says "OPDS output per Q6".

**Context**: OPDS 1.2 has no rating element. OPDS 2.0 / Readium Web Publication Manifest metadata
has no standard rating field either; a custom `belongsTo`-style extension would be ignored by
readers.

**Question**: Should feeds carry ratings?

**Options**:
- [ ] **A**: No, REST and MCP only (recommended)
- [ ] **B**: An `ef:rating` extension element in OPDS 1.2 and a custom metadata key in OPDS 2.0

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): no rating in OPDS 1.2 or 2.0.
```

---

### Q7: Rating facet and filter ⚠️ Medium

**Issue**: The API section makes `order_by=rating` and `rating_min` firm but the facet optional.

**Context**: `/api/v1/entries/facets` (IP-020) already computes several facets per request; a
rating facet adds one grouped count over `entries`.

**Question**: Include a `rating` facet in v1?

**Options**:
- [ ] **A**: Yes, buckets "4 and up", "3 and up", … (recommended if the portal has a filter for it)
- [ ] **B**: No, only `order_by` and `rating_min`

**Answer**:
```
```

**Resolution**:
```
Implemented as B for now: `rating_min` and `order_by=-rating`, no facet. A is conditional on a
portal filter that does not exist yet; adding the facet later is additive.
```

---

### Q8: Plain or Bayesian average for sorting? ⚠️ Medium

**Issue**: Technical Details, "Average and sorting".

**Context**: With about a thousand books and few ratings each, a plain average lets single ratings
dominate `order_by=rating`.

**Question**: Which value does `order_by=rating` sort by, and which is shown?

**Options**:
- [ ] **A**: Show the plain average; sort by a Bayesian average (recommended)
- [ ] **B**: Plain average for both, count as tie-breaker
- [ ] **C**: Plain average, and entries with fewer than N ratings sort last

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): `rating.average` is the plain average (2 decimals);
order_by=-rating sorts by (PRIOR_WEIGHT·PRIOR_MEAN + rating_sum) / (PRIOR_WEIGHT + rating_count),
defaults EVILFLOWERS_REVIEWS_PRIOR_MEAN=3.0, EVILFLOWERS_REVIEWS_PRIOR_WEIGHT=5 (constants rather than
the catalog-wide mean, so no extra aggregate query per request).
```

---

### Q9: Are reviewer names public? ⚠️ Medium

**Issue**: The API section does not say what a review exposes about its author.

**Context**: A review reveals what someone reads. Users come from STU LDAP with real names.

**Question**: What does `GET …/reviews` show about the reviewer?

**Options**:
- [ ] **A**: Name and surname (recommended if users are told when they write a review)
- [ ] **B**: A display name or initials only
- [ ] **C**: Anonymous to everyone but moderators

**Answer**:
```
```

**Resolution**:
```
Implemented as A (pending confirmation): reviews carry `user: {id, name, surname}`. The portal
should tell users their name is shown when they write a review.
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-11-09 | Claude AI (for jdubec) | Initial draft from issue #57 and IP-003 Q6 |
| 2026-11-09 | Claude AI (for jdubec) | Added Review Questions section |
| 2026-11-09 | Claude AI (for jdubec) | Implemented with the recommended option of each Review Question (Q7 as B); Resolutions record what was built, Answers await confirmation. `order_by=-rating` is best first; aggregate columns excluded from `Entry.save()`; `post_delete` receiver for cascades; `recompute_ratings --check` |
| 2026-11-09 | Claude AI (for jdubec) | Opened elvira-portal#32 for the portal widgets |
