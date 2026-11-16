"""HTTP client of `decider` (Laya, `POST /v1/systemone/batch`; contract knowledge#64).

One question set, up to 64 text states per call. Every call names the checkpoint (`EVILFLOWERS_DECIDER_MODEL`,
`multilingual`): without it Laya routes by itself and may load its English checkpoint. The model that answered
(`model` in the response) is what the catalog stores as `decider_model`, so a new revision re-asks.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

#: States per `/v1/systemone/batch` call (the server's limit).
BATCH = 64
#: Characters of one state the server accepts.
MAX_STATE = 50_000


class DeciderError(Exception):
    """The decider is off, unreachable, slow or answered something unusable."""


@dataclass(frozen=True)
class Answer:
    choice: Optional[str]
    #: Probability of the chosen answer (`probabilities[choice]`, else `confidence`).
    p: Optional[float]
    probabilities: Dict[str, float]


def enabled() -> bool:
    return bool(settings.EVILFLOWERS_DECIDER_URL)


def choice(instructions: str, criteria: Dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def _answer(raw: Optional[dict]) -> Answer:
    raw = raw or {}
    probabilities = {str(key): float(value) for key, value in (raw.get("probabilities") or {}).items()}
    picked = raw.get("choice")
    p = probabilities.get(picked)
    if p is None and raw.get("confidence") is not None:
        p = float(raw["confidence"])
    return Answer(choice=picked, p=p, probabilities=probabilities)


def ask(states: Sequence, questions: Dict[str, dict]) -> tuple[List[Dict[str, Answer]], str]:
    """Answers per state (in order) and the model that answered. Raises DeciderError."""
    if not enabled():
        raise DeciderError("EVILFLOWERS_DECIDER_URL is not set")
    if not states:
        return [], ""
    if len(states) > BATCH:
        raise DeciderError(f"at most {BATCH} states per call, got {len(states)}")
    payload = {
        "model": settings.EVILFLOWERS_DECIDER_MODEL,
        "states": [state[:MAX_STATE] if isinstance(state, str) else state for state in states],
        "questions": questions,
    }
    try:
        response = requests.post(
            f"{settings.EVILFLOWERS_DECIDER_URL}/v1/systemone/batch",
            json=payload,
            timeout=settings.EVILFLOWERS_DECIDER_TIMEOUT,
        )
    except requests.RequestException as error:
        raise DeciderError(f"decider unreachable: {error}") from error
    if response.status_code != 200:
        raise DeciderError(f"decider answered {response.status_code}: {response.text[:300]}")
    try:
        body = response.json()
        results = body["results"]
    except (ValueError, KeyError, TypeError) as error:
        raise DeciderError("decider answer without `results`") from error
    if len(results) != len(states):
        raise DeciderError(f"decider answered {len(results)} of {len(states)} states")
    model = ""
    answers = []
    for result in results:
        result = result or {}
        model = model or str(result.get("model") or body.get("model") or "")
        answers.append({key: _answer(value) for key, value in (result.get("answers") or {}).items()})
    return answers, model or settings.EVILFLOWERS_DECIDER_MODEL
