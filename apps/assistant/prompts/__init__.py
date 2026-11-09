"""The assistant's prompts, as versioned template files (IP-024 §6, #116).

Each prompt is a Markdown file in this directory, rendered by `render(name, **values)`:

- `{{ name }}` is replaced with a value the caller passes — tool names and limits come from the code that
  defines them, so renaming a tool cannot leave a stale name in a prompt. An unknown placeholder raises.
- `{{> segment }}` includes `_segment.md`, a piece shared between prompts (the citation contract).
- The file's final newline is not part of the prompt.

A deployment may replace any template without a code change by pointing
`EVILFLOWERS_ASSISTANT_PROMPTS_DIR` at a directory holding files of the same names; a file missing there falls
back to the one shipped here. Prompts are rendered once, at import, so a change takes effect on restart — and it
changes the cached prompt prefix, so run `assistant_eval` before and after.

`fingerprint()` hashes what the model is told before the conversation starts (system prompt and tool list). It is
stored on each assistant message and in the evaluation report, so a change in behaviour can be traced to a change
in prompt.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from django.conf import settings

BUNDLED = Path(__file__).resolve().parent

_INCLUDE = re.compile(r"\{\{>\s*([a-z0-9_]+)\s*\}\}")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")

#: UI languages the reader can switch between, by code.
LANGUAGE_NAMES = {"sk": "Slovak", "en": "English"}

#: Appended wherever the answer must be Slovak: small models drift into Czech.
NOT_CZECH = " It must be Slovak, never Czech."


class PromptError(Exception):
    """A template is missing or names a value it was not given."""


def _directories():
    override = getattr(settings, "EVILFLOWERS_ASSISTANT_PROMPTS_DIR", None)
    if override:
        yield Path(override)
    yield BUNDLED


def load(name: str) -> str:
    """The raw template `name.md`, from the override directory if it has one."""
    for directory in _directories():
        path = directory / f"{name}.md"
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            return text[:-1] if text.endswith("\n") else text
    raise PromptError(f"No prompt template '{name}.md'")


def render(name: str, **values) -> str:
    def include(match: re.Match) -> str:
        return load(f"_{match.group(1)}")

    text = _INCLUDE.sub(include, load(name))

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise PromptError(f"Prompt '{name}' needs a value for '{key}'")
        return str(values[key])

    return _PLACEHOLDER.sub(replace, text)


def fingerprint(*parts) -> str:
    """A short, stable hash of prompt parts (strings or JSON-serialisable values)."""
    digest = hashlib.sha256()
    for part in parts:
        text = part if isinstance(part, str) else json.dumps(part, sort_keys=True, ensure_ascii=False)
        digest.update(text.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:12]


def language_name(language: Optional[str]) -> Optional[str]:
    """'Slovak' for 'sk', 'sk-SK'…; None for a language the UI does not offer."""
    return LANGUAGE_NAMES.get((language or "").split("-")[0].lower())


def not_czech(language: Optional[str]) -> str:
    """The Czech guard when the answer must be Slovak, else nothing."""
    return NOT_CZECH if language_name(language) == "Slovak" else ""
