"""Typed `MENTIONS` and `SHOWS` (#206): does a section explain a concept or only name it, and does a passage next to
a figure refer to it.

Both edges of graph `library` come from string and proximity matching: a concept named once in a list counts like
one a section explains, a figure is linked to a passage whether or not the passage refers to it. The night sweep
(after `decider-edge-sweep`, before `graph-rebuild`) asks the decider per pair and keeps the answer in
`MentionRelation` / `ShowsRelation`, keyed by run-independent keys. The projection writes `kind` and `p` on the
edge and leaves out an edge whose answer is in the rule's `hide` list (`named`, `no`) with p >= `upper`
(`EVILFLOWERS_DECIDER_BANDS` rules `mention` and `shows`). Without thresholds nothing is left out.

- `MENTIONS`: a `choice` (`explains` / `uses` / `named`); state = the section's paragraph that names the concept
  (else the section's text), at most 1 500 characters, and the concept's label.
- `SHOWS`: a `choice` (`yes` / `no`), "Does this passage refer to the figure?"; state = the referring paragraph and
  the figure's printed caption, text only (Laya, not decider-vision). Asked as a two-answer `choice`, the question
  type the catalog already calls, rather than `noul`.
"""

import logging
import time
from collections import Counter
from typing import Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.db import transaction

from apps.decider import client
from apps.decider.models import Band, MentionRelation, ShowsRelation

logger = logging.getLogger(__name__)

#: Characters of a paragraph in a state.
TEXT_CHARS = 1500

MENTION_QUESTION = {
    "mention": client.choice(
        "How does the passage treat the concept?",
        {
            MentionRelation.Relation.EXPLAINS: "the passage explains the concept",
            MentionRelation.Relation.USES: "uses it without explaining",
            MentionRelation.Relation.NAMED: "only names it",
        },
    )
}
SHOWS_QUESTION = {
    "shows": client.choice(
        "Does this passage refer to the figure?",
        {ShowsRelation.Answer.YES: "yes, it refers to the figure", ShowsRelation.Answer.NO: "no"},
    )
}

#: Answers hidden by default when a rule has thresholds but no `hide` list.
DEFAULT_HIDE = {"mention": [MentionRelation.Relation.NAMED], "shows": [ShowsRelation.Answer.NO]}


def band(task: str, answer: Optional[str], p: Optional[float], config: Optional[dict] = None) -> str:
    """`hide` for an answer in the rule's `hide` list with p >= upper, `review` with p >= lower, else `keep`."""
    config = settings.EVILFLOWERS_DECIDER_BANDS if config is None else config
    rule = (config or {}).get(task)
    if not isinstance(rule, dict) or "upper" not in rule or p is None:
        return Band.KEEP
    if answer not in (rule.get("hide") or DEFAULT_HIDE[task]):
        return Band.KEEP
    if p >= float(rule["upper"]):
        return Band.HIDE
    if "lower" in rule and p >= float(rule["lower"]):
        return Band.REVIEW
    return Band.KEEP


# -- what the projection reads ------------------------------------------------------------------------------


def mention_lookup(section: str, concept_keys: Iterable[str]) -> Dict[str, MentionRelation]:
    return {
        relation.concept_key: relation
        for relation in MentionRelation.objects.filter(section_key=section, concept_key__in=list(concept_keys))
    }


def shows_lookup(pairs: Iterable[Tuple[str, str]]) -> Dict[Tuple[str, str], ShowsRelation]:
    pairs = {pair for pair in pairs if pair[0] and pair[1]}
    if not pairs:
        return {}
    paragraphs = {paragraph for paragraph, _ in pairs}
    figures = {figure for _, figure in pairs}
    return {
        (relation.paragraph_key, relation.figure_key): relation
        for relation in ShowsRelation.objects.filter(paragraph_key__in=paragraphs, figure_key__in=figures)
        if (relation.paragraph_key, relation.figure_key) in pairs
    }


# -- candidates ---------------------------------------------------------------------------------------------


def _mention_pairs() -> List[dict]:
    """Every (section, concept) a summary of an active run names: the graph's `MENTIONS` edges."""
    from apps.assistant.models import SectionSummary
    from apps.core.models import Entry
    from apps.graph.projection import concept_key, concept_subject, section_key

    subjects: Dict[str, str] = {}
    pairs, seen = [], set()
    summaries = (
        SectionSummary.objects.filter(run__is_active=True)
        .order_by("entry_id", "section")
        .values_list("run_id", "entry_id", "section", "concepts")
    )
    for run_id, entry_id, section, concepts in summaries.iterator():
        if str(entry_id) not in subjects:
            subjects[str(entry_id)] = concept_subject(Entry.objects.get(pk=entry_id))
        key = section_key(entry_id, section)
        for item in concepts or []:
            if not isinstance(item, dict) or not item.get("term"):
                continue
            concept = concept_key(str(item["term"]), subjects[str(entry_id)])
            if not concept or (key, concept) in seen:
                continue
            seen.add((key, concept))
            pairs.append({"run": run_id, "section": section, "key": (key, concept), "term": str(item["term"])[:120]})
    return pairs


def _shows_pairs() -> List[dict]:
    """Every (paragraph, figure) cross-reference of an active run with both locator keys (the `SHOWS` edges)."""
    from apps.extraction.models import Reference

    rows = (
        Reference.objects.filter(
            run__is_active=True,
            region__isnull=False,
            paragraph__locator_key__isnull=False,
            region__locator_key__isnull=False,
        )
        .exclude(region__band="hide")
        .order_by("run_id", "paragraph__seq")
        .values_list("paragraph__locator_key", "region__locator_key", "paragraph__text", "region__printed_caption")
    )
    pairs, seen = [], set()
    for paragraph_key, figure_key, text, caption in rows.iterator():
        if (paragraph_key, figure_key) in seen:
            continue
        seen.add((paragraph_key, figure_key))
        pairs.append({"key": (paragraph_key, figure_key), "text": text or "", "caption": caption or ""})
    return pairs


def _queue(pairs: List[dict], known: Dict[tuple, object], model: Optional[str], limit: int) -> List[dict]:
    """Pairs without an answer first, then pairs another model answered (oldest first)."""
    untyped, stale = [], []
    for pair in pairs:
        relation = known.get(pair["key"])
        if relation is None:
            untyped.append(pair)
        elif model and relation.decider_model != model:
            stale.append((relation.updated_at, pair))
    stale.sort(key=lambda item: item[0])
    return (untyped + [pair for _, pair in stale])[:limit]


def mention_candidates(model: Optional[str], limit: int) -> List[dict]:
    pairs = _mention_pairs()
    sections = {pair["key"][0] for pair in pairs}
    known = {
        (relation.section_key, relation.concept_key): relation
        for relation in MentionRelation.objects.filter(section_key__in=sections)
    }
    return _queue(pairs, known, model, limit)


def shows_candidates(model: Optional[str], limit: int) -> List[dict]:
    pairs = _shows_pairs()
    known = shows_lookup(pair["key"] for pair in pairs)
    return _queue(pairs, known, model, limit)


# -- states -------------------------------------------------------------------------------------------------


def _mention_texts(batch: List[dict]) -> Dict[tuple, str]:
    """Per pair, the section's first paragraph that names the term, else the section's opening text."""
    from apps.assistant.citations import fold
    from apps.decider.edges import _section_texts
    from apps.extraction.models import Paragraph

    texts: Dict[tuple, str] = {}
    sides = {(pair["run"], pair["section"]) for pair in batch}
    paragraphs: Dict[tuple, List[str]] = {}
    for run_id, section, text in (
        Paragraph.objects.searchable()
        .filter(run_id__in={run for run, _ in sides}, section__in={section for _, section in sides})
        .order_by("seq")
        .values_list("run_id", "section", "text")
    ):
        if (run_id, section) in sides:
            paragraphs.setdefault((run_id, section), []).append(text)
    missing = []
    for pair in batch:
        term = fold(pair["term"])
        found = next(
            (text for text in paragraphs.get((pair["run"], pair["section"]), []) if term and term in fold(text)), None
        )
        if found:
            texts[pair["key"]] = " ".join(found.split())[:TEXT_CHARS]
        else:
            missing.append(pair)
    if missing:
        fallback = _section_texts((pair["run"], pair["section"]) for pair in missing)
        for pair in missing:
            texts[pair["key"]] = fallback.get((pair["run"], pair["section"]), "")[:TEXT_CHARS]
    return texts


def mention_state(pair: dict, text: str) -> dict:
    return {"body": f"Concept: {pair['term']}\n\nPassage: {text}"}


def shows_state(pair: dict) -> dict:
    return {
        "body": f"Figure caption: {' '.join(pair['caption'].split())[:500]}\n\n"
        f"Passage: {' '.join(pair['text'].split())[:TEXT_CHARS]}"
    }


# -- storing ------------------------------------------------------------------------------------------------


def _store_mention(pair: dict, answer: client.Answer, model: str) -> Optional[MentionRelation]:
    if answer.choice not in MentionRelation.Relation.values:
        logger.warning("decider.mention_unknown_choice choice=%s", answer.choice)
        return None
    section, concept = pair["key"]
    relation, _ = MentionRelation.objects.update_or_create(
        section_key=section,
        concept_key=concept,
        defaults={
            "relation": answer.choice,
            "p": answer.p,
            "probabilities": answer.probabilities,
            "band": band("mention", answer.choice, answer.p),
            "decider_model": model,
        },
    )
    return relation


def _store_shows(pair: dict, answer: client.Answer, model: str) -> Optional[ShowsRelation]:
    if answer.choice not in ShowsRelation.Answer.values:
        logger.warning("decider.shows_unknown_choice choice=%s", answer.choice)
        return None
    paragraph, figure = pair["key"]
    relation, _ = ShowsRelation.objects.update_or_create(
        paragraph_key=paragraph,
        figure_key=figure,
        defaults={
            "answer": answer.choice,
            "p": answer.p,
            "probabilities": answer.probabilities,
            "band": band("shows", answer.choice, answer.p),
            "decider_model": model,
        },
    )
    return relation


# -- the sweep ----------------------------------------------------------------------------------------------


def _latest(model_class) -> Optional[str]:
    return model_class.objects.order_by("-updated_at").values_list("decider_model", flat=True).first()


def _run_task(task: str, queue: List[dict], started: float, seconds: int) -> dict:
    question = MENTION_QUESTION if task == "mention" else SHOWS_QUESTION
    store = _store_mention if task == "mention" else _store_shows
    stored, by_answer, hidden, failed, position, model = 0, Counter(), 0, 0, 0, None
    while position < len(queue) and time.monotonic() - started < seconds:
        batch = queue[position : position + client.BATCH]
        position += len(batch)
        if task == "mention":
            texts = _mention_texts(batch)
            states = [mention_state(pair, texts.get(pair["key"], "")) for pair in batch]
        else:
            states = [shows_state(pair) for pair in batch]
        try:
            answers, model = client.ask(states, question)
        except client.DeciderError as error:
            logger.warning("decider.%s_sweep_failed error=%s", task, error)
            failed += len(batch)
            break
        with transaction.atomic():
            for pair, answer in zip(batch, answers):
                relation = store(pair, answer.get(task) or client.Answer(None, None, {}), model)
                if relation is None:
                    failed += 1
                    continue
                stored += 1
                by_answer[relation.relation if task == "mention" else relation.answer] += 1
                hidden += relation.band == Band.HIDE
    return {
        "typed": stored,
        "hidden": hidden,
        "failed": failed,
        "left": max(len(queue) - position, 0),
        "answers": dict(by_answer),
        "model": model,
    }


def sweep(limit: Optional[int] = None, seconds: Optional[int] = None) -> dict:
    """Type up to `limit` `MENTIONS` and `limit` `SHOWS` pairs within one budget of `seconds` (untyped first)."""
    from evil_flowers_catalog import domain_metrics

    limit = settings.EVILFLOWERS_DECIDER_MENTION_LIMIT if limit is None else limit
    seconds = settings.EVILFLOWERS_DECIDER_SWEEP_SECONDS if seconds is None else seconds
    started = time.monotonic()
    report = {}
    for task, model_class, candidates in (
        ("mention", MentionRelation, mention_candidates),
        ("shows", ShowsRelation, shows_candidates),
    ):
        task_started = time.monotonic()
        queue = candidates(_latest(model_class), limit)
        report[task] = _run_task(task, queue, started, seconds)
        duration = round(time.monotonic() - task_started, 1)
        report[task]["seconds"] = duration
        domain_metrics.record_decider_sweep(task, duration, report[task]["answers"])
        logger.info(
            "decider.%s_sweep typed=%s hidden=%s failed=%s left=%s seconds=%s answers=%s model=%s",
            task,
            report[task]["typed"],
            report[task]["hidden"],
            report[task]["failed"],
            report[task]["left"],
            duration,
            report[task]["answers"],
            report[task]["model"],
        )
    return report
