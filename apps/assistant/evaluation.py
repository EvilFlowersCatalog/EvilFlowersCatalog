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

    outcome.failures = failures
    outcome.passed = not failures
    return outcome


def run_case(user, case: dict) -> Outcome:
    """Run one case as a real turn in a throw-away chat, then delete the chat."""
    from apps.assistant import turn
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
            elif event == "error":
                outcome.error = data.get("error")
        outcome.total_s = round(time.monotonic() - started, 2)
        outcome.tokens = _tokens(chat)
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

    return {
        "cases": len(outcomes),
        "passed": len(passed),
        "czech_answers": sum(1 for outcome in outcomes if outcome.answer_language == "cs"),
        "errors": sum(1 for outcome in outcomes if outcome.error),
        "first_token_s_p50": percentile(first, 0.5),
        "first_token_s_p95": percentile(first, 0.95),
        "total_s_p50": percentile(total, 0.5),
        "total_s_p95": percentile(total, 0.95),
        "tokens": sum(outcome.tokens for outcome in outcomes),
    }


def report(outcomes: List[Outcome], settings_snapshot: dict) -> dict:
    return {"settings": settings_snapshot, "summary": summary(outcomes), "cases": [asdict(o) for o in outcomes]}


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
