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
- `grounded`: a question about what publications *say*. A text tool (`search_library_text`, `read_pdf_page`
  or a bound chat's document tools) must be called, so an answer from the model's own knowledge fails even
  when it names a real publication (#130: `sk-passage` credited a chapter to a book that has none).
- `reads_pages`: the answer drew on pages of the chat's publication (the `pages` frame of a bound chat).

A case may bind its chat to a publication, as the portal's "ask about this book" does: `entry_title` is looked
up among the publications the user can see (titles, unlike ids, are the same on every instance), and the turn
gets that publication's open-access PDF, as `ChatMessages` would give it (#130 C).

Whatever the case expects, an answer citing a label it was never given (`[E9]` when only E1-E3 were issued)
fails: that label is an invention, and catching it needs no gold data. So does a Slovak answer with a word
only Czech has (`knihovna`): the majority vote of `answer_language` misses a single slip (#130 B).

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
from urllib.parse import urlsplit

from django.conf import settings
from django.test import RequestFactory

#: Letters and words that occur in Czech but not Slovak, and the reverse. Enough to
#: tell the two apart in an answer of a few sentences, which is all this needs.
_CZECH = re.compile(r"[řůě]|\b(jsou|který|která|které|také|nebo|můžete|není|knihy jsou|půjčit)\b", re.IGNORECASE)
_SLOVAK = re.compile(r"[äľĺŕô]|\b(sú|ktorý|ktorá|ktoré|tiež|alebo|môžete|nie je|požičať)\b", re.IGNORECASE)
_SLAVIC = re.compile(r"[áčďéíňóšťúýž]", re.IGNORECASE)

#: Czech words with no Slovak homograph, as they slip into otherwise Slovak answers. Kept to words that are
#: never Slovak, so a single one is a failure (`knihovna`: #130, `sk-author`).
_CZECH_ONLY = re.compile(
    r"\b(knihovn[aěuyo]\w*|jsou|který|která|které|můžete|půjč\w*|vypůjč\w*|děkuji|bohužel|ještě|již)\b",
    re.IGNORECASE,
)

#: Tools that read the text of a publication; a `grounded` case must call one.
TEXT_TOOLS = (
    "search_library_text",
    "read_pdf_page",
    "search_pdf",
    "search_paragraphs",
    "list_regions",
    # IP-030: the evidence pack, links and study read publications' text too.
    "gather_evidence",
    "follow",
    "study",
)

#: Model rounds a library question should need (search, maybe refine, display, answer).
#: More is scored as a loop; on CPU each round costs seconds of prompt evaluation.
MAX_ROUNDS_OK = 5


def czech_words(text: str) -> List[str]:
    """Czech-only words in `text`, lowercased, each once."""
    return list(dict.fromkeys(match.group(0).lower() for match in _CZECH_ONLY.finditer(text or "")))


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
    #: Czech-only words in a Slovak answer (#130).
    czech_words: List[str] = field(default_factory=list)
    #: Pages of the chat's publication the answer drew on (bound cases).
    pages_read: int = 0
    #: IP-030: model time of the turn, the study artefact's mode, figures cited with a crop, the evidence pack.
    model_seconds: float = 0.0
    study: Optional[str] = None
    figures_shown: int = 0
    evidence: int = 0
    #: The turn failed and stored its notice (`ChatMessage.error`).
    failed_row: bool = False


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
    elif wanted == "sk" and outcome.czech_words:
        failures.append(f"Czech words: {', '.join(outcome.czech_words)}")
    if expect.get("grounded") and not called & set(TEXT_TOOLS):
        failures.append("content answered without reading any publication")
    if expect.get("reads_pages") and not outcome.pages_read:
        failures.append("no page of the publication read")
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
    # IP-030 cases.
    if expect.get("shows_figure") and not outcome.figures_shown:
        failures.append("no figure shown")
    if expect.get("study") and outcome.study != expect["study"]:
        failures.append(f"no {expect['study']} card" + (f" ({outcome.study})" if outcome.study else ""))
    if expect.get("max_rounds") and outcome.rounds > expect["max_rounds"]:
        failures.append(f"{outcome.rounds} rounds, wanted at most {expect['max_rounds']}")
    for phrase in expect.get("refuses_not") or []:
        if phrase.lower() in (outcome.answer or "").lower():
            failures.append(f"said {phrase!r}")

    outcome.failures = failures
    outcome.passed = not failures
    return outcome


def evaluation_request(method: str, path: str = "/assistant/v1/evaluation"):
    """A request on the instance's own host, so tools that build absolute URLs work.

    RequestFactory's default host is `testserver`, which a deployment's ALLOWED_HOSTS
    rejects: every `get_entry` in an evaluation run raised DisallowedHost (#121).
    """
    base = urlsplit(getattr(settings, "EVILFLOWERS_BASE_URL", "") or "")
    host = base.netloc or "localhost"
    factory = RequestFactory(HTTP_HOST=host)
    return getattr(factory, method)(path, secure=base.scheme == "https")


def run_case(user, case: dict, verify: bool = False) -> Outcome:
    """Run one case as a real turn in a throw-away chat, then delete the chat.

    With `verify`, every citation of the answer is checked against its passage (#117) before the chat goes.
    """
    from apps.assistant import support, turn
    from apps.assistant.evidence import Ledger
    from apps.assistant.models import Chat

    outcome = Outcome(id=str(case.get("id") or case["question"][:40]), question=case["question"])
    request = evaluation_request("post")
    request.user = user
    try:
        entry = _case_entry(request, case)
    except CaseError as error:
        # Scored as an error rather than stopping the run: the other cases still say something.
        outcome.error = str(error)
        return score(case, outcome)
    chat = Chat.objects.create(user=user, title=f"[evaluation] {outcome.id}", entry=entry)
    document = _case_document(request, entry)

    started = time.monotonic()
    frames = turn.run(request, chat, case["question"], document=document, language=case.get("language"))
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
                outcome.figures_shown = sum(1 for c in citations if c.get("image_url"))
            elif event == "pages":
                outcome.pages_read = len(data.get("pages") or [])
            elif event == "study":
                outcome.study = data.get("mode")
            elif event == "evidence":
                outcome.evidence = len(data.get("sources") or [])
            elif event == "error":
                outcome.error = data.get("error")
                outcome.failed_row = bool(data.get("message_id"))
        outcome.total_s = round(time.monotonic() - started, 2)
        outcome.tokens = _tokens(chat)
        outcome.model_seconds = _model_seconds(chat)
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
    outcome.czech_words = czech_words(outcome.answer)
    return score(case, outcome)


class CaseError(Exception):
    """A case names a publication this instance does not hold."""


def _case_entry(request, case: dict):
    """The publication a bound case is about (`entry_title`), among those the user can see; None otherwise."""
    title = case.get("entry_title")
    if not title:
        return None
    from apps.api.filters.entries import EntryFilter
    from apps.core.models import Entry

    entry = (
        EntryFilter({}, queryset=Entry.objects.all(), request=request)
        .qs.filter(title__icontains=title)
        .order_by("created_at")
        .first()
    )
    if entry is None:
        raise CaseError(f"No visible publication titled like {title!r}")
    return entry


def _case_document(request, entry):
    """What `ChatMessages._chat_document` gives a chat about `entry` without a reader's copy."""
    if entry is None:
        return None
    from apps.assistant.pdf import DocumentUnavailable, open_access_document

    try:
        return open_access_document(request, entry)
    except DocumentUnavailable:
        return None


def _timed(frames, outcome: Outcome, started: float):
    for frame in frames:
        if outcome.first_token_s is None and frame.startswith("event: chunk"):
            outcome.first_token_s = round(time.monotonic() - started, 2)
        yield frame


def _tokens(chat) -> int:
    from django.db.models import Sum

    return chat.messages.aggregate(tokens=Sum("tokens_used"))["tokens"] or 0


def _model_seconds(chat) -> float:
    from django.db.models import Sum

    return round(chat.messages.aggregate(seconds=Sum("model_seconds"))["seconds"] or 0.0, 2)


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
        "answers_with_czech_words": sum(1 for outcome in outcomes if outcome.czech_words),
        "errors": sum(1 for outcome in outcomes if outcome.error),
        "cases_with_citations": sum(1 for outcome in outcomes if outcome.citations),
        "unknown_labels": sum(len(outcome.unknown_labels) for outcome in outcomes),
        "first_token_s_p50": percentile(first, 0.5),
        "first_token_s_p95": percentile(first, 0.95),
        "total_s_p50": percentile(total, 0.5),
        "total_s_p95": percentile(total, 0.95),
        "tokens": sum(outcome.tokens for outcome in outcomes),
        # IP-030 success criteria: rounds per question, model time, citations, failed turns stored.
        "rounds_p50": percentile(sorted(outcome.rounds for outcome in outcomes), 0.5),
        "model_seconds": round(sum(outcome.model_seconds for outcome in outcomes), 1),
        "model_seconds_p50": percentile(sorted(outcome.model_seconds for outcome in outcomes), 0.5),
        "citation_rate": round(sum(1 for o in outcomes if o.citations) / len(outcomes), 3) if outcomes else None,
        "figures_shown": sum(outcome.figures_shown for outcome in outcomes),
        "errors_without_row": sum(1 for outcome in outcomes if outcome.error and not outcome.failed_row),
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
        from apps.assistant.llm import client as make_client

        client = make_client()

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


def export_feedback(limit: int) -> List[dict]:
    """Rated answers with the question before them (IP-030 Q13), for a person to anonymise into cases.

    Printed to standard output only: the reason is a reader's free text and may hold personal data.
    """
    from apps.assistant.models import ChatMessage

    rated = (
        ChatMessage.objects.filter(role=ChatMessage.Role.ASSISTANT, feedback__isnull=False)
        .exclude(chat__title__startswith="[evaluation]")
        .order_by("-created_at")[:limit]
    )
    items = []
    for answer in rated:
        question = (
            ChatMessage.objects.filter(
                chat_id=answer.chat_id, role=ChatMessage.Role.USER, created_at__lt=answer.created_at
            )
            .order_by("-created_at")
            .values_list("text", flat=True)
            .first()
        )
        items.append(
            {
                "question": question or "",
                "answer": answer.text,
                "rating": (answer.feedback or {}).get("rating"),
                "reason": (answer.feedback or {}).get("reason") or "",
                "citations": [c.get("label") for c in answer.citations or []],
                "prompt_fingerprint": answer.prompt_fingerprint,
            }
        )
    return items
