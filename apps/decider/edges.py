"""Typed section pairs (#190): the decider says how two similar sections relate; the graph and readers use it.

`SectionSimilarity` comes from vectors only: it says "near", not why. The nightly sweep asks, per pair, how
section B relates to section A (same idea, A prerequisite of B, B example of A, loosely related, unrelated) from
both sections' summaries (or their first paragraphs), and keeps the answer in `SectionRelation`, keyed by the
graph's section keys so similarity rebuilds do not wipe it. An `unrelated` answer above the `edge` threshold
(`EVILFLOWERS_DECIDER_BANDS`) hides the pair from readers; without thresholds nothing is hidden.

What readers see is relative to the section they asked about (`perspective`): `same`, `prerequisite` (read the
other first), `next` (the other builds on this one), `example` (the other is an example of this one), `general`
(this one is an example of the other) or `loose`.
"""

import logging
import time
from collections import Counter
from typing import Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.db import transaction

from apps.decider import client
from apps.decider.models import Band, SectionRelation

logger = logging.getLogger(__name__)

QUESTION = {
    "relation": client.choice(
        "How is section B related to section A?",
        {
            SectionRelation.Relation.SAME: "explains the same idea",
            SectionRelation.Relation.PREREQUISITE: "A is a prerequisite of B",
            SectionRelation.Relation.EXAMPLE: "B is an example of A",
            SectionRelation.Relation.LOOSE: "loosely related",
            SectionRelation.Relation.NONE: "unrelated",
        },
    )
}

#: Characters of each section in a state: the summary, else its first paragraphs.
SECTION_CHARS = 1500

#: Graph edges per relation, drawn from (A) to (B) or the other way (`apps.graph.projection`).
TYPED_EDGES = {
    SectionRelation.Relation.SAME: ("SAME_IDEA_AS", False),
    SectionRelation.Relation.PREREQUISITE: ("PREREQUISITE_OF", False),
    SectionRelation.Relation.EXAMPLE: ("EXAMPLE_OF", True),  # B is an example of A: (B)-[:EXAMPLE_OF]->(A)
}

#: `relation` as seen from A, and as seen from B.
_FROM_A = {"same": "same", "pre": "next", "example": "example", "loose": "loose", "none": "none"}
_FROM_B = {"same": "same", "pre": "prerequisite", "example": "general", "loose": "loose", "none": "none"}


def band(relation: str, p: Optional[float], config: Optional[dict] = None) -> str:
    """`hide` for an answer in the `edge` rule's `hide` list with p >= upper, `review` with p >= lower."""
    config = settings.EVILFLOWERS_DECIDER_BANDS if config is None else config
    rule = (config or {}).get("edge")
    if not isinstance(rule, dict) or "upper" not in rule or p is None:
        return Band.KEEP
    if relation not in (rule.get("hide") or [SectionRelation.Relation.NONE]):
        return Band.KEEP
    if p >= float(rule["upper"]):
        return Band.HIDE
    if "lower" in rule and p >= float(rule["lower"]):
        return Band.REVIEW
    return Band.KEEP


def relation_factor(relation: Optional[SectionRelation], config: Optional[dict] = None, weights=None) -> float:
    """#203: what a pair's SIMILAR_TO weight is multiplied by in `related_sections`.

    The factor of its relation (`EVILFLOWERS_DECIDER_RELATION_WEIGHTS`, e.g. `{"same": 1.2, "loose": 0.7}`), only
    when the decider's p reaches the `edge` rule's `upper` (`EVILFLOWERS_DECIDER_BANDS`). Untyped pairs, pairs below
    the threshold, relations without a weight and a deployment without thresholds (until knowledge#65) get 1.
    """
    if relation is None or relation.p is None:
        return 1.0
    config = settings.EVILFLOWERS_DECIDER_BANDS if config is None else config
    weights = settings.EVILFLOWERS_DECIDER_RELATION_WEIGHTS if weights is None else weights
    rule = (config or {}).get("edge")
    if not isinstance(rule, dict) or "upper" not in rule or relation.p < float(rule["upper"]):
        return 1.0
    try:
        return float((weights or {}).get(relation.relation, 1.0))
    except (TypeError, ValueError):
        return 1.0


def perspective(relation: SectionRelation, key: str) -> str:
    """The relation as the reader of section `key` sees the other section."""
    return (_FROM_A if relation.a_key == key else _FROM_B).get(relation.relation, relation.relation)


def lookup(pairs: Iterable[Tuple[str, str]]) -> Dict[frozenset, SectionRelation]:
    """`{frozenset({a, b}): relation}` for the pairs (either order) that have one."""
    pairs = list(pairs)
    keys = {key for pair in pairs for key in pair}
    if not keys:
        return {}
    found = {}
    for relation in SectionRelation.objects.filter(a_key__in=keys, b_key__in=keys):
        found[frozenset((relation.a_key, relation.b_key))] = relation
    wanted = {frozenset(pair) for pair in pairs}
    return {pair: relation for pair, relation in found.items() if pair in wanted}


def hidden_pairs(pairs: Iterable[Tuple[str, str]]) -> set:
    return {pair for pair, relation in lookup(pairs).items() if relation.band == Band.HIDE}


# -- the sweep ----------------------------------------------------------------------------------------------


def _pairs() -> List[dict]:
    """Every similar pair of two active runs, with both section keys (A = the row's source)."""
    from apps.extraction.models import SectionSimilarity
    from apps.graph.projection import section_key

    rows = (
        SectionSimilarity.objects.filter(source__is_active=True, target__is_active=True)
        .values_list(
            "source_id",
            "source__acquisition__entry_id",
            "source_section",
            "target_id",
            "target__acquisition__entry_id",
            "target_section",
            "score",
        )
        .order_by("-score")
    )
    return [
        {
            "a": (source_run, source_section, section_key(source_entry, source_section)),
            "b": (target_run, target_section, section_key(target_entry, target_section)),
            "score": score,
        }
        for source_run, source_entry, source_section, target_run, target_entry, target_section, score in rows
    ]


def candidates(model: Optional[str], limit: int) -> List[dict]:
    """Pairs without a relation first (most similar first), then pairs another model typed (oldest first)."""
    pairs = _pairs()
    known = lookup([(pair["a"][2], pair["b"][2]) for pair in pairs])
    untyped, stale = [], []
    for pair in pairs:
        relation = known.get(frozenset((pair["a"][2], pair["b"][2])))
        if relation is None:
            untyped.append(pair)
        elif model and relation.decider_model != model:
            # Asked again in the orientation it was asked before, so the answers stay comparable.
            if relation.a_key != pair["a"][2]:
                pair = {**pair, "a": pair["b"], "b": pair["a"]}
            stale.append((relation.updated_at, pair))
    stale.sort(key=lambda item: item[0])
    return (untyped + [pair for _, pair in stale])[:limit]


def _section_texts(sides: Iterable[Tuple]) -> Dict[Tuple, str]:
    """`{(run_id, section): text}`: the section's summary, else its first paragraphs."""
    from apps.assistant.models import SectionSummary
    from apps.extraction.models import Paragraph

    sides = set(sides)
    texts: Dict[Tuple, str] = {}
    runs = {run for run, _ in sides}
    for run_id, section, summary in SectionSummary.objects.filter(run_id__in=runs).values_list(
        "run_id", "section", "summary"
    ):
        if (run_id, section) in sides and summary:
            texts[(run_id, section)] = summary[:SECTION_CHARS]
    missing = sides - set(texts)
    for run_id, section in missing:
        parts, size = [], 0
        for text in (
            Paragraph.objects.searchable()
            .filter(run_id=run_id, section=section)
            .order_by("seq")
            .values_list("text", flat=True)
        ):
            parts.append(text)
            size += len(text) + 1
            if size >= SECTION_CHARS:
                break
        texts[(run_id, section)] = " ".join(parts)[:SECTION_CHARS]
    return texts


def state(pair: dict, texts: Dict[Tuple, str]) -> dict:
    a_run, a_section, _ = pair["a"]
    b_run, b_section, _ = pair["b"]
    return {
        "body": (
            f"Section A: {a_section}\n{texts.get((a_run, a_section), '')}\n\n"
            f"Section B: {b_section}\n{texts.get((b_run, b_section), '')}"
        )
    }


def _store(pair: dict, answer: client.Answer, model: str) -> Optional[SectionRelation]:
    if answer.choice not in SectionRelation.Relation.values:
        logger.warning("decider.edge_unknown_choice choice=%s", answer.choice)
        return None
    a_key, b_key = pair["a"][2], pair["b"][2]
    with transaction.atomic():
        # One row per pair, in the orientation it was asked.
        SectionRelation.objects.filter(a_key=b_key, b_key=a_key).delete()
        relation, _ = SectionRelation.objects.update_or_create(
            a_key=a_key,
            b_key=b_key,
            defaults={
                "relation": answer.choice,
                "p": answer.p,
                "probabilities": answer.probabilities,
                "band": band(answer.choice, answer.p),
                "decider_model": model,
            },
        )
    return relation


def latest_model() -> Optional[str]:
    return SectionRelation.objects.order_by("-updated_at").values_list("decider_model", flat=True).first()


def sweep(limit: Optional[int] = None, seconds: Optional[int] = None) -> dict:
    """Type up to `limit` pairs within `seconds`; untyped pairs first. Returns counts for the job ledger."""
    limit = settings.EVILFLOWERS_DECIDER_EDGE_LIMIT if limit is None else limit
    seconds = settings.EVILFLOWERS_DECIDER_SWEEP_SECONDS if seconds is None else seconds
    started = time.monotonic()
    model = latest_model()
    queue = candidates(model, limit)
    typed, by_relation, hidden, failed = 0, Counter(), 0, 0
    position = 0
    while position < len(queue) and time.monotonic() - started < seconds:
        batch = queue[position : position + client.BATCH]
        position += len(batch)
        texts = _section_texts([pair["a"][:2] for pair in batch] + [pair["b"][:2] for pair in batch])
        try:
            answers, answered_by = client.ask([state(pair, texts) for pair in batch], QUESTION)
        except client.DeciderError as error:
            logger.warning("decider.edge_sweep_failed error=%s", error)
            failed += len(batch)
            break
        # A new revision answers from now on; the pairs the old one typed are due from the next night on
        # (`latest_model`), after the untyped ones.
        model = answered_by
        for pair, answer in zip(batch, answers):
            relation = _store(pair, answer.get("relation") or client.Answer(None, None, {}), model)
            if relation is None:
                failed += 1
                continue
            typed += 1
            by_relation[relation.relation] += 1
            hidden += relation.band == Band.HIDE
    duration = round(time.monotonic() - started, 1)
    report = {
        "typed": typed,
        "hidden": hidden,
        "failed": failed,
        "left": max(len(queue) - position, 0),
        "relations": dict(by_relation),
        "model": model,
        "seconds": duration,
    }
    logger.info(
        "decider.edge_sweep typed=%s hidden=%s failed=%s left=%s seconds=%s relations=%s model=%s",
        typed,
        hidden,
        failed,
        report["left"],
        duration,
        dict(by_relation),
        model,
    )
    from evil_flowers_catalog import domain_metrics

    domain_metrics.record_decider_sweep("edge", duration, by_relation)
    return report
