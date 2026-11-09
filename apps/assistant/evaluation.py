"""Offline evaluation of the assistant against a model (#104, IP-022).

A case is a question plus what a good turn looks like:

    {"id": "sk-borrow-now", "question": "Ktoré knihy o strojovom učení si môžem hneď požičať?",
     "language": "sk", "expect": {"tools_any": ["search_entries"], "displays": true}}

`expect` keys (all optional):

- `tools_any`: at least one of these tools is called.
- `tools_all`: every one of these tools is called.
- `tools_none`: none of these is called.
- `displays`: publications are (true) / are not (false) shown as cards.
- `answer_language`: "sk" or "en"; defaults to the case's `language`.
- `cites`: the answer cites at least one evidence label that resolves (IP-024).
- `cites_entries`: every one of these publication ids is cited.
- `cites_any_entry`: at least one of these publication ids is cited (gold cases, #118).

Whatever the case expects, an answer citing a label it was never given (`[E9]` when only E1-E3 were issued)
fails: that label is an invention, and catching it needs no gold data.

Each case runs as a real turn (`turn.run`) in a throw-away chat of the given
user, against whatever model the settings point at, and is scored on what the
turn did: tools, cards, language (Slovak vs Czech), loops, errors and timings.
Nothing here judges whether the recommended publications are *good*; that stays
a human reading of the report.
"""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Optional

from django.test import RequestFactory

#: Letters and words that occur in Czech but not Slovak, and the reverse. Enough to
#: tell the two apart in an answer of a few sentences, which is all this needs.
_CZECH = re.compile(r"[řůě]|\b(jsou|který|která|které|také|nebo|můžete|není|knihy jsou|půjčit)\b", re.IGNORECASE)
_SLOVAK = re.compile(r"[äľĺŕô]|\b(sú|ktorý|ktorá|ktoré|tiež|alebo|môžete|nie je|požičať)\b", re.IGNORECASE)
_SLAVIC = re.compile(r"[áčďéíňóšťúýž]", re.IGNORECASE)

#: Model rounds a library question should need (search, maybe refine, display, answer).
#: More is scored as a loop; on CPU each round costs seconds of prompt evaluation.
MAX_ROUNDS_OK = 5


def answer_language(text: str) -> Optional[str]:
    """'sk', 'cs', 'en' or None (too short to tell)."""
    text = text or ""
    if len(text.strip()) < 12:
        return None
    czech, slovak = len(_CZECH.findall(text)), len(_SLOVAK.findall(text))
    if czech > slovak:
        return "cs"
    if slovak or _SLAVIC.search(text):
        return "sk"
    return "en"


@dataclass
class Outcome:
    id: str
    question: str
    passed: bool = False
    failures: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    displayed: int = 0
    rounds: int = 0
    answer: str = ""
    answer_language: Optional[str] = None
    error: Optional[str] = None
    first_token_s: Optional[float] = None
    total_s: float = 0.0
    tokens: int = 0
    #: Publication ids of the resolved citations, in order of first appearance.
    cited_entries: List[str] = field(default_factory=list)
    citations: int = 0
    unknown_labels: List[str] = field(default_factory=list)
    #: The prompt the case ran with (#116): system prompt + tool list, hashed.
    prompt_fingerprint: str = ""
    #: Support verdicts per citation, with `run --verify` (#117).
    support: List[dict] = field(default_factory=list)


def parse_frames(frames: Iterable[str]) -> List[tuple]:
    """SSE frames as `(event, data)` pairs."""
    events = []
    for frame in frames:
        event, data = None, None
        for line in frame.strip().splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[len("data: ") :])
                except json.JSONDecodeError:
                    data = None
        if event:
            events.append((event, data or {}))
    return events


def score(case: dict, outcome: Outcome) -> Outcome:
    expect = case.get("expect") or {}
    called = set(outcome.tools)
    failures = []

    if outcome.error:
        failures.append(f"error: {outcome.error}")
    if expect.get("tools_any") and not called & set(expect["tools_any"]):
        failures.append(f"none of {expect['tools_any']} called")
    for name in expect.get("tools_all") or []:
        if name not in called:
            failures.append(f"{name} not called")
    for name in expect.get("tools_none") or []:
        if name in called:
            failures.append(f"{name} called")
    if "displays" in expect and bool(outcome.displayed) != bool(expect["displays"]):
        failures.append("cards shown" if outcome.displayed else "no cards shown")
    wanted = expect.get("answer_language") or case.get("language")
    if wanted and outcome.answer_language and outcome.answer_language != wanted:
        failures.append(f"answered in {outcome.answer_language}, wanted {wanted}")
    if outcome.rounds > MAX_ROUNDS_OK:
        failures.append(f"{outcome.rounds} model rounds (loop?)")
    if outcome.unknown_labels:
        failures.append(f"cited unknown labels {', '.join(outcome.unknown_labels)}")
    if expect.get("cites") and not outcome.citations:
        failures.append("no citation")
    missing = [entry_id for entry_id in expect.get("cites_entries") or [] if entry_id not in outcome.cited_entries]
    if missing:
        failures.append(f"not cited: {', '.join(missing)}")
    acceptable = expect.get("cites_any_entry") or []
    if acceptable and not set(acceptable) & set(outcome.cited_entries):
        failures.append("no gold publication cited")

    outcome.failures = failures
    outcome.passed = not failures
    return outcome


def run_case(user, case: dict, verify: bool = False) -> Outcome:
    """Run one case as a real turn in a throw-away chat, then delete the chat.

    With `verify`, every citation of the answer is checked against its passage (#117) before the chat goes.
    """
    from apps.assistant import support, turn
    from apps.assistant.evidence import Ledger
    from apps.assistant.models import Chat

    outcome = Outcome(id=str(case.get("id") or case["question"][:40]), question=case["question"])
    request = RequestFactory().post("/assistant/v1/evaluation")
    request.user = user
    chat = Chat.objects.create(user=user, title=f"[evaluation] {outcome.id}")

    started = time.monotonic()
    frames = turn.run(request, chat, case["question"], language=case.get("language"))
    try:
        for event, data in parse_frames(_timed(frames, outcome, started)):
            if event == "status" and data.get("state") == "thinking":
                outcome.rounds += 1
            elif event == "status" and data.get("state") == "tool":
                outcome.tools.append(data.get("tool") or "")
            elif event == "entries":
                outcome.tools.append("displayBooks")
                outcome.displayed += len(data.get("entry_ids") or [])
            elif event == "message":
                outcome.answer = data.get("text") or outcome.answer
            elif event == "citations":
                citations = data.get("citations") or []
                outcome.citations = len(citations)
                outcome.cited_entries = list(dict.fromkeys(str(c.get("entry_id")) for c in citations))
                outcome.unknown_labels = list(data.get("unknown") or [])
            elif event == "error":
                outcome.error = data.get("error")
        outcome.total_s = round(time.monotonic() - started, 2)
        outcome.tokens = _tokens(chat)
        outcome.prompt_fingerprint = (
            chat.messages.exclude(prompt_fingerprint="")
            .order_by("-created_at")
            .values_list("prompt_fingerprint", flat=True)
            .first()
            or ""
        )
        if verify and outcome.citations:
            outcome.support = support.as_dicts(support.verify(outcome.answer, Ledger.for_chat(chat)))
    finally:
        chat.delete()

    outcome.answer_language = answer_language(outcome.answer)
    return score(case, outcome)


def _timed(frames, outcome: Outcome, started: float):
    for frame in frames:
        if outcome.first_token_s is None and frame.startswith("event: chunk"):
            outcome.first_token_s = round(time.monotonic() - started, 2)
        yield frame


def _tokens(chat) -> int:
    from django.db.models import Sum

    return chat.messages.aggregate(tokens=Sum("tokens_used"))["tokens"] or 0


def summary(outcomes: List[Outcome]) -> dict:
    passed = [outcome for outcome in outcomes if outcome.passed]
    first = sorted(outcome.first_token_s for outcome in outcomes if outcome.first_token_s is not None)
    total = sorted(outcome.total_s for outcome in outcomes)

    def percentile(values, p):
        if not values:
            return None
        return values[min(len(values) - 1, int(round(p * (len(values) - 1))))]

    verdicts: dict = {}
    for outcome in outcomes:
        for item in outcome.support:
            verdicts[item["verdict"]] = verdicts.get(item["verdict"], 0) + 1

    return {
        "support": verdicts,
        "cases": len(outcomes),
        "passed": len(passed),
        "czech_answers": sum(1 for outcome in outcomes if outcome.answer_language == "cs"),
        "errors": sum(1 for outcome in outcomes if outcome.error),
        "cases_with_citations": sum(1 for outcome in outcomes if outcome.citations),
        "unknown_labels": sum(len(outcome.unknown_labels) for outcome in outcomes),
        "first_token_s_p50": percentile(first, 0.5),
        "first_token_s_p95": percentile(first, 0.95),
        "total_s_p50": percentile(total, 0.5),
        "total_s_p95": percentile(total, 0.95),
        "tokens": sum(outcome.tokens for outcome in outcomes),
        # One per tool list (signed in or not); two runs compare only when these match.
        "prompt_fingerprints": sorted({outcome.prompt_fingerprint for outcome in outcomes} - {""}),
    }


def report(outcomes: List[Outcome], settings_snapshot: dict) -> dict:
    return {"settings": settings_snapshot, "summary": summary(outcomes), "cases": [asdict(o) for o in outcomes]}


def export_pairs(limit: int) -> List[dict]:
    """(sentence, cited passage) pairs from real answers, for a person to label `supported` (#117).

    `supported` is left null; fill it with true (the passage states or directly implies what the sentence
    attributes to it) or false, following the rubric in `prompts/support.md`.
    """
    from apps.assistant import support
    from apps.assistant.evidence import Ledger
    from apps.assistant.models import ChatMessage

    rows = (
        ChatMessage.objects.filter(role=ChatMessage.Role.ASSISTANT, citations__isnull=False)
        .exclude(chat__title__startswith="[evaluation]")
        .select_related("chat")
        .order_by("-created_at")
    )
    ledgers, exported = {}, []
    for row in rows.iterator(chunk_size=50):
        ledger = ledgers.get(row.chat_id) or ledgers.setdefault(row.chat_id, Ledger.for_chat(row.chat))
        for pair in support.pairs(row.text, ledger):
            source = ledger.sources.get(pair.label) or {}
            exported.append(
                {
                    "id": f"{row.pk}:{pair.label}",
                    "sentence": pair.sentence,
                    "context": pair.context,
                    "passage": pair.passage,
                    "entry_id": source.get("entry_id"),
                    "page": source.get("page"),
                    "supported": None,
                }
            )
            if len(exported) >= limit:
                return exported
    return exported


def held_out(pair_id: str) -> bool:
    """A fixed half of the labelled pairs, by id, kept for testing a threshold chosen on the other half."""
    import hashlib

    return hashlib.sha256(pair_id.encode()).digest()[0] % 2 == 1


def calibrate(labelled: List[dict], threshold: Optional[int] = None, client=None) -> dict:
    """Score labelled pairs with the verifier; precision/recall per threshold on the tuning half.

    Choose the threshold from the tuning half's table and write it into IP-024 first. Then run again with
    `threshold` to get the held-out half's numbers — the ones that decide whether the check may gate anything.
    """
    from apps.assistant import support

    if client is None:
        from apps.assistant.llm import OllamaClient

        client = OllamaClient()

    tuning, testing, skipped = [], [], 0
    for item in labelled:
        if item.get("supported") is None:
            skipped += 1
            continue
        pair = support.Pair("E1", item["sentence"], item.get("context") or "", item["passage"])
        verdict, score, _reason, _tokens = support.check_pair(client, pair)
        row = (bool(item["supported"]), score)
        (testing if held_out(str(item["id"])) else tuning).append(row)

    result = {"tuning_pairs": len(tuning), "held_out_pairs": len(testing), "unlabelled": skipped}
    result["tuning"] = support.calibrate(tuning)
    if threshold is not None:
        result["held_out"] = support.calibrate(testing, thresholds=[threshold])[0]
    return result


def export_questions(limit: int) -> List[dict]:
    """Recent real user questions as case skeletons, for a human to add `expect` to (#105 §1)."""
    from apps.assistant.models import ChatMessage

    rows = (
        ChatMessage.objects.filter(role=ChatMessage.Role.USER)
        .exclude(chat__title__startswith="[evaluation]")
        .exclude(text="")
        .order_by("-created_at")
        .values_list("text", flat=True)[: limit * 3]
    )
    seen, cases = set(), []
    for text in rows:
        key = " ".join(text.lower().split())
        if key in seen:
            continue
        seen.add(key)
        cases.append(
            {"id": f"chat-{len(cases) + 1}", "question": text, "language": answer_language(text), "expect": {}}
        )
        if len(cases) >= limit:
            break
    return cases
