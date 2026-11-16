"""Which language a message or an answer is in: Slovak, Czech or English (#187).

Two users, one rule:

- the turn pins the answer's language after each reader message (`services.language_instruction`). A pin that
  only said "the language of the reader's last message" was not enough for the CPU model: an English question in
  the Slovak UI, and even in the English UI, was answered in Slovak (dev eval 2026-11-15, `en-shelf`,
  `en-asks-in-slovak-ui`, `en-portal-tour`). The pin now names the language;
- `assistant_eval` scores the answer's language. It called an English answer Slovak for a single accented letter
  ("Elvíra", `en-showcase`).

So the decision is by weight: English function words against Slovak and Czech words and words with Slavic
diacritics. Czech against Slovak is decided by their own marker words and letters, as before. Deterministic, so a
pin computed from a stored message never changes the replayed prefix (#138).
"""

import re
from typing import Optional

_WORD = re.compile(r"\w+", re.UNICODE)

#: English words that are not also Slovak words (`a`, `i`, `to`, `do`, `on` are both).
_ENGLISH = re.compile(
    r"\b(the|and|is|are|was|were|what|which|how|why|where|who|whom|does|did|you|your|of|me|my|an|in|for|with|"
    r"can|could|would|should|about|show|find|tell|this|that|these|those|it|its|have|has|there|book|books|please|"
    r"from|any|anything|something|recommend|give|quiz|explain|need|want|library|i'm|i've|let|lets)\b",
    re.IGNORECASE,
)
#: Slovak function words (most are Czech too; Czech against Slovak is decided below).
_SLAVIC_WORDS = re.compile(
    r"\b(je|sú|su|čo|co|ako|aké|aká|aký|ktoré|ktorá|ktorý|máte|mám|mi|ma|sa|si|v|vo|na|o|pre|z|zo|kde|prosím|"
    r"prosim|knihy|kniha|knihu|nejaké|nejake|niečo|nieco|alebo|tiež|nie|áno|ukáž|ukaz|nájdi|najdi|odporuč|môžem|"
    r"môžete|mate|aj|ale|že|ze|jsou|který|která|které|také|nebo|můžete|není)\b",
    re.IGNORECASE,
)
_DIACRITIC = re.compile(r"[áäčďéíĺľňóôŕšťúýžřůě]", re.IGNORECASE)

#: Letters and words that occur in Czech but not Slovak, and the reverse.
CZECH = re.compile(r"[řůě]|\b(jsou|který|která|které|také|nebo|můžete|není|knihy jsou|půjčit)\b", re.IGNORECASE)
SLOVAK = re.compile(r"[äľĺŕô]|\b(sú|ktorý|ktorá|ktoré|tiež|alebo|môžete|nie je|požičať)\b", re.IGNORECASE)


def guess(text: Optional[str], min_words: int = 2) -> Optional[str]:
    """'sk', 'cs', 'en', or None when `text` is too short or carries no signal ("ok", a title)."""
    text = text or ""
    words = _WORD.findall(text)
    if len(words) < min_words:
        return None
    english = len(_ENGLISH.findall(text))
    slavic = len(_SLAVIC_WORDS.findall(text)) + sum(1 for word in words if _DIACRITIC.search(word))
    if not english and not slavic:
        return None
    if english > slavic:
        return "en"
    if slavic > english:
        return "cs" if len(CZECH.findall(text)) > len(SLOVAK.findall(text)) else "sk"
    return None


def answer_language(text: Optional[str], fallback: Optional[str] = None) -> Optional[str]:
    """The language to answer `text` in: 'sk' or 'en'. Czech is answered in Slovak; no signal → `fallback`."""
    found = guess(text)
    if found == "cs":
        return "sk"
    if found in ("sk", "en"):
        return found
    code = (fallback or "").split("-")[0].lower()
    return code or None
