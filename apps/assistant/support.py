"""Does a cited passage support the sentence that cites it? (IP-024 §4, #117)

An evidence label (`evidence.py`) proves the passage was retrieved, not that it says what the sentence claims.
This asks the model one small, constrained question per (sentence, cited label) pair — after FActScore and
VeriScore, and monad-knowledge's `pipeline/support.py` — and returns a verdict and a 0-100 score.

It never runs inside a live turn: on CPU every pair is a prompt evaluation of a few hundred tokens. It runs from
`assistant_eval` (`run --verify`, `calibrate`) until a threshold has been calibrated on human-labelled pairs; only
then is it decided whether it runs on live traffic and whether readers see it (IP-024 Q5).

A failed or refused call is `unverified`, never one of the three verdicts.
"""

import logging
import re
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from apps.assistant import prompts, structured
from apps.assistant.evidence import Ledger, labels_in

logger = logging.getLogger(__name__)

SUPPORTED, PARTIAL, UNSUPPORTED, UNVERIFIED = "supported", "partial", "unsupported", "unverified"
VERDICTS = (SUPPORTED, PARTIAL, UNSUPPORTED)

SYSTEM_PROMPT = prompts.render("support")

SUPPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "score"],
}

#: Characters of a passage given to the verifier; library passages are cut at 600 already.
MAX_PASSAGE_CHARS = 1500

# ----------------------------------------------------------------- sentences

#: Abbreviations after which a full stop does not end a sentence (Slovak, Czech, English), lower case.
ABBREVIATIONS = {
    "napr", "tzv", "atď", "resp", "tj", "t", "j", "porov", "pozri", "str", "s", "č", "obr", "tab", "kap", "roč",
    "zv", "vyd", "prof", "doc", "ing", "mgr", "phd", "dr", "rndr", "judr", "mudr", "tzn", "např", "apod", "viz",
    "e.g", "i.e", "eg", "ie", "etc", "et al", "al", "fig", "vs", "cf", "p", "pp", "vol", "no", "ch", "sec", "eq",
    "mr", "mrs", "ms", "st", "jr",
}  # fmt: skip

#: A candidate boundary: end punctuation, then optional closing quotes/brackets, then whitespace.
_BOUNDARY = re.compile(r"([.!?…]+[\"'“”»)\]]*)(\s+)")
_WORD_BEFORE = re.compile(r"([\w.]+)$", re.UNICODE)
_ONLY_LABELS = re.compile(r"^(?:\[\s*[EF]\d+(?:\s*[,;]\s*[EF]\d+)*\s*\]\s*)+[.!?]?$", re.IGNORECASE)
_LEADING_LABELS = re.compile(r"(?:\[\s*[EF]\d+(?:\s*[,;]\s*[EF]\d+)*\s*\]\s*)+", re.IGNORECASE)


def split_sentences(text: str) -> List[str]:
    """Sentences of an answer, aware of abbreviations, initials, decimals and trailing citation labels.

    A label written after the full stop ("… pressure. [E2]") belongs to the sentence before it.
    """
    text = " ".join((text or "").split())
    if not text:
        return []

    sentences: List[str] = []
    start = 0
    for match in _BOUNDARY.finditer(text):
        end = match.end(1)
        head = text[start : match.start(1)]
        word = _WORD_BEFORE.search(head)
        token = word.group(1).lower().rstrip(".") if word else ""
        following = text[match.end() : match.end() + 1]
        if match.group(1) == "." and (
            token in ABBREVIATIONS
            or (len(token) == 1 and token.isalpha())  # an initial: "J. Novák"
            or (token.isdigit() and following.isdigit())
            or (following and following.islower())  # "napr. pri" — a lower-case continuation
        ):
            continue
        # Labels right after the full stop ("… falls. [E2] Speed …") still cite this sentence.
        labels = _LEADING_LABELS.match(text, match.end())
        if labels:
            end = labels.end()
        sentences.append(text[start:end].strip())
        start = labels.end() if labels else match.end()
    if start < len(text):
        sentences.append(text[start:].strip())

    merged: List[str] = []
    for sentence in sentences:
        if merged and _ONLY_LABELS.match(sentence):
            merged[-1] = f"{merged[-1]} {sentence}"
        elif sentence:
            merged.append(sentence)
    return merged


# ----------------------------------------------------------------- pairs and verdicts


@dataclass
class Pair:
    label: str
    sentence: str
    #: The sentences before and after, to resolve references only.
    context: str
    passage: str


@dataclass
class Verdict:
    label: str
    sentence: str
    verdict: str
    score: Optional[int] = None
    reason: str = ""
    entry_id: Optional[str] = None
    page: Optional[int] = None


def pairs(text: str, ledger: Ledger) -> List[Pair]:
    """(sentence, cited label) pairs whose passage text is known, in answer order."""
    sentences = split_sentences(text)
    found: List[Pair] = []
    for index, sentence in enumerate(sentences):
        context = " ".join(sentences[max(0, index - 1) : index] + sentences[index + 1 : index + 2])
        for label in labels_in(sentence):
            passage = ledger.texts.get(label)
            if passage:
                found.append(Pair(label, sentence, context, passage[:MAX_PASSAGE_CHARS]))
    return found


def _messages(pair: Pair) -> List[dict]:
    request = (
        f'Sentence:\n"""\n{pair.sentence}\n"""\n\n'
        f'Cited passage [{pair.label}]:\n"""\n{pair.passage}\n"""\n\n'
        f'Sentences around it (for references only):\n"""\n{pair.context or "-"}\n"""'
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": request}]


def _valid(data: dict) -> bool:
    return data.get("verdict") in VERDICTS


def check_pair(client, pair: Pair) -> Tuple[str, Optional[int], str, int]:
    """`(verdict, score, reason, tokens)` for one pair; `unverified` when the call fails or is unusable."""
    from apps.assistant.llm import LLMUnavailable

    content, tokens = "", 0
    try:
        for delta in client.chat(_messages(pair), tools=None, response_format=SUPPORT_SCHEMA):
            content += delta.content
            if delta.done:
                tokens = delta.total_tokens
    except LLMUnavailable as error:
        logger.warning("Support check unavailable: %s", error)
        return UNVERIFIED, None, "", tokens

    data = structured.parse_object(content, _valid)
    if data is None:
        return UNVERIFIED, None, "", tokens
    try:
        score = max(0, min(100, int(data.get("score"))))
    except (TypeError, ValueError):
        score = None
    return data["verdict"], score, str(data.get("reason") or "").strip(), tokens


def verify(text: str, ledger: Ledger, client=None) -> List[Verdict]:
    """A verdict for every citation of `text` whose passage is known."""
    if client is None:
        from apps.assistant.llm import client as make_client

        client = make_client()
    verdicts = []
    for pair in pairs(text, ledger):
        verdict, score, reason, _tokens = check_pair(client, pair)
        source = ledger.sources.get(pair.label) or {}
        verdicts.append(
            Verdict(pair.label, pair.sentence, verdict, score, reason, source.get("entry_id"), source.get("page"))
        )
    return verdicts


def summarise(verdicts: Iterable[Verdict]) -> Dict[str, int]:
    counts = {name: 0 for name in (*VERDICTS, UNVERIFIED)}
    for verdict in verdicts:
        counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
    return counts


def as_dicts(verdicts: Iterable[Verdict]) -> List[dict]:
    return [asdict(verdict) for verdict in verdicts]


# ----------------------------------------------------------------- calibration


def calibrate(scored: List[Tuple[bool, Optional[int]]], thresholds=range(0, 101, 5)) -> List[dict]:
    """Precision and recall of "score ≥ threshold means supported" against human labels.

    `scored` is `(human says supported, model score)`. A pair the model could not score counts as not passing
    the gate, which is the conservative reading for a gate.
    """
    positives = sum(1 for label, _score in scored if label)
    rows = []
    for threshold in thresholds:
        passed = [(label, score) for label, score in scored if score is not None and score >= threshold]
        true_positive = sum(1 for label, _score in passed if label)
        rows.append(
            {
                "threshold": threshold,
                "passed": len(passed),
                "precision": round(true_positive / len(passed), 3) if passed else None,
                "recall": round(true_positive / positives, 3) if positives else None,
            }
        )
    return rows
