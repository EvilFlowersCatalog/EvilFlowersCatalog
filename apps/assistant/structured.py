"""The JSON object in a model's answer (IP-024 §2).

Explain, highlight and semantic selection ask the model for a single JSON object. With Ollama's `format` the
answer is constrained to a schema, but `format` cannot be combined with tools, so the answer that ends a
tool-using loop is free text and may come back fenced, with prose around it, with a trailing comma or with
typographic quotes. `parse_object` tries, in order: a fenced block, the first object that decodes, and the same
two after a small repair. `valid` rejects an object of the wrong shape so the next candidate gets a chance.
"""

import json
import re
from typing import Callable, Iterator, Optional

#: Sent once when an answer held no usable object; the retry also sets `format` and offers no tools.
RETRY_INSTRUCTION = (
    "Your previous answer was not the JSON object asked for. Answer again with only that JSON object: "
    "no explanation, no code fences."
)

_FENCED = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'"})

_decoder = json.JSONDecoder()


def _objects(text: str) -> Iterator[dict]:
    """Every JSON object that decodes, starting from each `{`, left to right."""
    start = text.find("{")
    while start != -1:
        try:
            value, _end = _decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            yield value
        start = text.find("{", start + 1)


def _candidates(text: str) -> Iterator[dict]:
    for block in _FENCED.findall(text):
        yield from _objects(block)
    yield from _objects(text)


def _without_trailing_commas(text: str) -> str:
    return _TRAILING_COMMA.sub(r"\1", text)


def parse_object(content: Optional[str], valid: Optional[Callable[[dict], bool]] = None) -> Optional[dict]:
    """The first JSON object in `content` that `valid` accepts, or None.

    Repairs are staged: trailing commas first, typographic quotes only after that. Slovak text quotes with „…“
    inside string values, and swapping those for `"` would break an object that only had a trailing comma.
    """
    text = content or ""
    commas = _without_trailing_commas(text)
    for source in (text, commas, commas.translate(_QUOTES)):
        for candidate in _candidates(source):
            if valid is None or valid(candidate):
                return candidate
    return None
