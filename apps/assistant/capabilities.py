"""The capability card and starter prompts (IP-030 § API), without a model.

"Showcase what you can do" came 6 times in 10 days, and each answer cost a minute of CPU for a static fact. The
portal shows this card instead. It lists what this session can do: shelf and loans only when signed in, reading
the open publication's figures and sections only when a chat is about one that is processed and readable.
"""

from typing import List, Optional

_TEXT = {
    "sk": {
        "search": ("Hľadať v knižnici", "Nájdi publikácie o strojovom učení, ktoré si môžem požičať hneď."),
        "passages": ("Odpovedať z textu s citáciami", "Čo hovorí knižnica o Bernoulliho rovnici?"),
        "figures": ("Ukázať obrázky a tabuľky", "Ukáž mi obrázok, ktorý to vysvetľuje."),
        "follow": ("Sledovať odkazy", "Ukáž mi ďalší odsek a podobné pasáže v iných knihách."),
        "walkthrough": ("Prejsť publikáciu po sekciách", "Prejdi so mnou túto knihu sekciu po sekcii."),
        "quiz": ("Otestovať ťa", "Otestuj ma z tejto kapitoly."),
        "concepts": ("Kľúčové pojmy", "Aké sú kľúčové pojmy tejto sekcie?"),
        "summary": ("Zhrnúť sekciu", "Zhrň mi túto kapitolu."),
        "overview": ("Prehľad knižnice", "O čom sú publikácie v knižnici?"),
        "loans": ("Moje výpožičky a polica", "Čo mám požičané a dokedy?"),
        "highlights": ("Zvýrazniť v čítačke", "Zvýrazni všetky definície v tejto kapitole."),
    },
    "en": {
        "search": ("Search the library", "Find books on machine learning I can borrow right now."),
        "passages": ("Answer from the text, with citations", "What does the library say about Bernoulli's equation?"),
        "figures": ("Show figures and tables", "Show me a figure that explains it."),
        "follow": ("Follow links", "Show me the next paragraph and similar passages in other books."),
        "walkthrough": ("Walk through a publication", "Walk me through this book section by section."),
        "quiz": ("Quiz you", "Quiz me on this chapter."),
        "concepts": ("Key concepts", "What are the key concepts of this section?"),
        "summary": ("Summarise a section", "Summarise this chapter for me."),
        "overview": ("Library overview", "What are the publications in the library mostly about?"),
        "loans": ("My loans and shelf", "What do I have on loan, and until when?"),
        "highlights": ("Highlight in the viewer", "Highlight every definition in this chapter."),
    },
}

#: Capabilities in display order; `about` ones need a readable, processed publication in the chat.
ORDER = ("search", "passages", "figures", "follow", "summary", "walkthrough", "quiz", "concepts", "overview")
ABOUT = ("summary", "walkthrough", "quiz", "concepts")


def card(language: Optional[str], authenticated: bool, about_readable: bool, bound: bool) -> dict:
    table = _TEXT.get((language or "sk").split("-")[0].lower(), _TEXT["en"])
    items: List[dict] = []
    for key in ORDER:
        if key in ABOUT and not about_readable:
            continue
        label, example = table[key]
        items.append({"id": key, "label": label, "example": example})
    if authenticated:
        label, example = table["loans"]
        items.append({"id": "loans", "label": label, "example": example})
    if bound:
        label, example = table["highlights"]
        items.append({"id": "highlights", "label": label, "example": example})
    starters = [
        item["example"]
        for item in items
        if item["id"]
        in (("walkthrough", "summary", "figures") if about_readable else ("search", "passages", "overview"))
    ]
    return {"capabilities": items, "starters": starters[:3]}
