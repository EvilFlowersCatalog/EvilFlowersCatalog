"""Suggestion chips after an answer (IP-030 § API), built from the ledger, never by the model.

Each is a message the reader can send with one click: the next part of a walkthrough, what a cited figure
shows, similar passages in other books for a cited passage, a quiz on the section just read. At most
`MAX_SUGGESTIONS`, in the reader's UI language.
"""

from typing import List, Optional

MAX_SUGGESTIONS = 3

_TEXT = {
    "sk": {
        "next_section": ("Ďalej: {section}", "Pokračuj v prechádzaní: {section}"),
        "next_part": ("Pokračovať", "Pokračuj ďalšou časťou sekcie {section}"),
        "figure": ("Čo zobrazuje {label}?", "Čo zobrazuje obrázok [{label}]?"),
        "similar": ("Podobné v iných knihách", "Nájdi podobné pasáže k [{label}] v iných knihách."),
        "quiz": ("Otestuj ma", "Otestuj ma z tejto sekcie: {section}"),
    },
    "en": {
        "next_section": ("Next: {section}", "Continue the walkthrough: {section}"),
        "next_part": ("Continue", "Continue with the next part of {section}"),
        "figure": ("What does {label} show?", "What does figure [{label}] show?"),
        "similar": ("Similar in other books", "Find passages similar to [{label}] in other books."),
        "quiz": ("Quiz me", "Quiz me on this section: {section}"),
    },
}


def _text(language: Optional[str], kind: str, **values) -> dict:
    table = _TEXT.get((language or "sk").split("-")[0].lower(), _TEXT["en"])
    label, message = table[kind]
    return {"label": label.format(**values)[:80], "message": message.format(**values), "kind": kind}


def build(citations: List[dict], study: Optional[dict], language: Optional[str]) -> List[dict]:
    suggestions: List[dict] = []
    artefact = (study or {}).get("data") or {}
    mode = (study or {}).get("mode")
    if mode == "walkthrough" and artefact.get("next"):
        upcoming = artefact["next"]
        if upcoming.get("section") == artefact.get("section"):
            suggestions.append(_text(language, "next_part", section=upcoming["section"][:60]))
        else:
            suggestions.append(_text(language, "next_section", section=upcoming["section"][:60]))
    if mode in ("walkthrough", "key_concepts") and artefact.get("section"):
        suggestions.append(_text(language, "quiz", section=artefact["section"][:60]))

    figure = next((c for c in citations if str(c.get("label", "")).startswith("F")), None)
    if figure is not None:
        suggestions.append(_text(language, "figure", label=figure["label"]))
    passage = next(
        (c for c in citations if str(c.get("label", "")).startswith("E") and c.get("kind") == "passage"), None
    )
    if passage is not None:
        suggestions.append(_text(language, "similar", label=passage["label"]))
        if passage.get("section") and not mode:
            suggestions.append(_text(language, "quiz", section=str(passage["section"])[:60]))
    return suggestions[:MAX_SUGGESTIONS]
