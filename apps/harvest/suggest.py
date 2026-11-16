"""#207: a suggested match for a harvested record that shares no identifier with the target catalog.

Exact-identifier matching (`apps.harvest.matching`) leaves a record without ISBN, ISSN, ČNB or DOI, or with
another edition's ISBN, `unmatched`, and a `create` source imports it as a duplicate of a book the catalog holds.
For such a record (and for one `ambiguous` between several entries sharing an identifier) the harvester:

1. takes up to `EVILFLOWERS_DECIDER_MATCH_CANDIDATES` entries of the target catalog: those sharing the identifier,
   else the best full-text hits of the record's title words and author surnames over `entries.search_vector`,
   published within a year of the record (or with no year);
2. asks the decider (`apps.decider.client`) one `choice`: is the record the same work as one of them, or `none`;
3. on an entry: holds the record as `suggested` and stores a `MatchSuggestion` for a librarian (accept = link,
   reject = create as new). Nothing is linked automatically until knowledge#65 sets a `harvest_match` threshold.

Without `EVILFLOWERS_DECIDER_URL`, in a dry run, on a `none` answer or after a decider error the record goes on
as before. One call per record: the criteria (the candidates) differ per record, and one decider call carries one
question set. A decider error stops the asking for the rest of the run.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Optional

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F, Q

from apps.core.models import Entry, EntryAuthor
from apps.harvest.records import SourceRecord

logger = logging.getLogger(__name__)

NONE = "none"
INSTRUCTIONS = "Is this record the same work as one of these entries?"
#: Words of the title and author surnames that go into the full-text query.
MAX_WORDS = 16
_WORD = re.compile(r"\w{3,}", re.UNICODE)


class Suggestion:
    """An answer naming an entry: what `MatchSuggestion` will store once the record's mirror row exists."""

    def __init__(self, entry_id: str, p, probabilities: dict, candidates: list[str], model: str):
        self.entry_id = entry_id
        self.p = p
        self.probabilities = probabilities
        self.candidates = candidates
        self.model = model

    def message(self) -> str:
        p = f"{self.p:.2f}" if self.p is not None else "?"
        return f"suggested match: entry {self.entry_id} (p={p}), waiting for a librarian"


def _year(value) -> Optional[int]:
    match = re.match(r"\s*(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def search_candidates(catalog, record: SourceRecord, limit: int) -> list[str]:
    """Entry ids of `catalog` best matching the record's title and authors, within a year of it."""
    from partial_date import PartialDate

    words: list[str] = []
    for word in _WORD.findall(" ".join([record.full_title or "", *(a.surname for a in record.authors)]).lower()):
        if word not in words:
            words.append(word)
    if not words:
        return []
    query = SearchQuery(" | ".join(words[:MAX_WORDS]), search_type="raw", config="evilflowers_simple")
    entries = Entry.objects.filter(catalog=catalog, search_vector=query)
    year = _year(record.published)
    if year:
        entries = entries.filter(
            Q(published_at__isnull=True)
            | Q(published_at__gte=PartialDate(str(year - 1)), published_at__lte=PartialDate(f"{year + 1}-12-31"))
        )
    ranked = entries.annotate(rank=SearchRank(F("search_vector"), query)).order_by("-rank", "pk")
    return [str(pk) for pk in ranked.values_list("pk", flat=True)[:limit]]


def describe_entries(entry_ids: list[str]) -> dict[str, str]:
    """`{id: "<title> / <authors> / <year> / <publisher>"}` in the order given."""
    rows = {str(entry.pk): entry for entry in Entry.objects.filter(pk__in=entry_ids)}
    authors: dict[str, list[str]] = {}
    for link in EntryAuthor.objects.filter(entry_id__in=entry_ids).select_related("author").order_by("position"):
        authors.setdefault(str(link.entry_id), []).append(link.author.full_name)
    described = {}
    for entry_id in entry_ids:
        entry = rows.get(entry_id)
        if entry is None:
            continue
        year = _year(entry.published_at) or ""
        described[entry_id] = (
            f"{entry.title} / {', '.join(authors.get(entry_id, []))} / {year} / {entry.publisher or ''}"
        )
    return described


def record_state(record: SourceRecord) -> dict:
    authors = "; ".join(f"{a.surname}, {a.name}".strip(", ") for a in record.authors)
    return {
        "body": (
            f"Title: {record.full_title or ''}\nAuthors: {authors}\nYear: {_year(record.published) or ''}\n"
            f"Publisher: {record.publisher or ''}\nLanguage: {record.language or ''}"
        )
    }


class Suggester:
    """Per run: asks the decider about records exact matching could not place."""

    def __init__(self, source, *, dry_run: bool):
        from apps.decider import client

        self.source = source
        self.active = client.enabled() and not dry_run
        self.answers: Counter = Counter()

    def suggest(self, record: SourceRecord, candidates: tuple = ()) -> Optional[Suggestion]:
        """A suggestion naming an entry, or None (no candidates, `none`, decider off or failing)."""
        if not self.active:
            return None
        from apps.decider import client

        limit = settings.EVILFLOWERS_DECIDER_MATCH_CANDIDATES
        offered = list(candidates)[:limit] or search_candidates(self.source.target_catalog, record, limit)
        criteria = describe_entries(offered)
        if not criteria:
            return None
        criteria[NONE] = "none of these entries"
        try:
            answers, model = client.ask([record_state(record)], {"match": client.choice(INSTRUCTIONS, criteria)})
        except client.DeciderError as error:
            logger.warning("harvest.match_suggestion_failed source=%s error=%s", self.source.slug, error)
            self.active = False  # the rest of the run goes on as without a decider
            return None
        answer = (answers[0] if answers else {}).get("match")
        if answer is None or answer.choice not in criteria or answer.choice == NONE:
            self.answers["none"] += 1
            return None
        self.answers["suggested"] += 1
        return Suggestion(answer.choice, answer.p, answer.probabilities, list(criteria)[:-1], model)

    def report(self) -> None:
        if self.answers:
            from evil_flowers_catalog import domain_metrics

            domain_metrics.record_decider_answers("harvest_match", dict(self.answers))


def store(row, suggestion: Suggestion):
    """The `MatchSuggestion` of a held record (one pending per record)."""
    from apps.harvest.models import MatchSuggestion

    obj, _created = MatchSuggestion.objects.update_or_create(
        record=row,
        status=MatchSuggestion.Status.PENDING,
        defaults={
            "source": row.source,
            "entry_id": suggestion.entry_id,
            "p": suggestion.p,
            "probabilities": suggestion.probabilities,
            "candidates": suggestion.candidates,
            "decider_model": suggestion.model,
        },
    )
    return obj


class SuggestionClosed(Exception):
    """The suggestion was already decided or superseded, or the entry is linked to another record."""


def _decide(suggestion, user, accepted: bool):
    """Apply a librarian's verdict under the source's lock; returns the record's new mirror row."""
    from django.db import transaction
    from django.utils import timezone

    from apps.harvest.apply import Applier, Outcome, run_user, store_record
    from apps.harvest.matching import MatchIndex, source_lock
    from apps.harvest.models import HarvestRecord, HarvestSource, MatchSuggestion

    source = suggestion.source
    with source_lock(source), transaction.atomic():
        suggestion = MatchSuggestion.objects.select_for_update().select_related("record").get(pk=suggestion.pk)
        if suggestion.status != MatchSuggestion.Status.PENDING:
            raise SuggestionClosed(f"The suggestion is {suggestion.status}.")
        row = suggestion.record
        record = SourceRecord.from_dict({**(row.source_record or {}), "record_id": row.record_id})
        index = MatchIndex(source.target_catalog, source.system_key)
        applier = Applier(source, index, user=run_user(source))
        if accepted:
            entry = Entry.objects.select_related("language").get(pk=suggestion.entry_id)
            current = (entry.identifiers or {}).get(source.system_key)
            if current and current != row.record_id:
                raise SuggestionClosed(f"The entry is already linked to {source.system_key}={current}.")
            if source.mode == HarvestSource.Mode.LINK_ONLY:
                outcome = Outcome(
                    HarvestRecord.Status.MATCHED, entry, "decider", changed=applier.link(entry, row.record_id)
                )
            else:
                outcome = applier.enrich(entry, record, "decider")
            outcome.message = outcome.message or "linked by a librarian (suggested match accepted)"
        elif source.mode == HarvestSource.Mode.CREATE:
            outcome = applier.create(record)
        else:
            outcome = Outcome(HarvestRecord.Status.UNMATCHED, message="suggested match rejected")
        suggestion.status = MatchSuggestion.Status.ACCEPTED if accepted else MatchSuggestion.Status.REJECTED
        suggestion.decided_by = user
        suggestion.decided_at = timezone.now()
        suggestion.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
        row = store_record(source, record, outcome, dry_run=False)
    from evil_flowers_catalog import domain_metrics

    domain_metrics.record_decider_answers("harvest_match", {"accepted" if accepted else "rejected": 1})
    return row


def accept(suggestion, user):
    """Link the record to the suggested entry (as a match of the source's mode: link, or enrich)."""
    return _decide(suggestion, user, accepted=True)


def reject(suggestion, user):
    """Not that entry: a `create` source creates the record as a new entry; other modes leave it unmatched."""
    return _decide(suggestion, user, accepted=False)


__all__ = [
    "Suggester",
    "Suggestion",
    "SuggestionClosed",
    "accept",
    "describe_entries",
    "reject",
    "search_candidates",
    "store",
]
