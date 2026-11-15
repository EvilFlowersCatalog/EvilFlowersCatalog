"""Showing the reader where things are in the portal (#167).

The portal (elvira-portal, `src/components/guide/guideRegistry.ts`) can take a reader to a page and spotlight the
control to look at, or walk them through several places (a tour). It names places by id only; the catalog never
knows routes or the DOM. The model picks ids from the map below with `show_in_portal`, the turn sends them as a
`navigate` frame, and the portal drops anything it does not know or the reader may not open.

The map lives in the tool's description, not in the system prompt: the prompt is rendered once per process, while
the tool list is per session, so the administration's places are only described to superusers (`staff`). The ids
are checked here too, so a made-up id never reaches the portal and the model is told which ones it got wrong.

`portal_path`, the page the reader is on, comes with each message and is replayed as a note on it
(`services.reader_notes`), like the viewer page.
"""

import re
from typing import Dict, List, Optional, Tuple

SHOW_IN_PORTAL = "show_in_portal"

#: Ids the portal accepts in one frame.
MAX_TARGETS = 6

#: Place id → what is there, for every signed-in reader; empty where the id says it. Order is the prompt's order.
PLACES: Dict[str, str] = {
    "home": "",
    "library": "browse all",
    "search": "",
    "advanced-search": "filters",
    "sort": "",
    "book": "a publication: borrow, read",
    "feeds": "collections",
    "categories": "subjects",
    "ai-chats": "past chats",
    "shelf": "saved",
    "history": "recently opened",
    "loans": "loans, reservations",
    "profile": "",
    "passphrase": "Thorium/DRM passphrase",
    "notifications": "e-mails",
    "menu-language": "",
    "menu-theme": "dark mode",
}

#: Menu entries: the portal also accepts these, e.g. to point at the menu itself. Not described to the model.
MENU = (
    "menu-home",
    "menu-library",
    "menu-feeds",
    "menu-categories",
    "menu-ai",
    "menu-shelf",
    "menu-history",
    "menu-loans",
    "menu-profile",
    "menu-help",
    "home-ai",
    "assistant-launcher",
)

#: The administration (superusers only).
ADMIN_PLACES: Dict[str, str] = {
    "admin-dashboard": "statistics",
    "admin-attention": "errors, stuck work",
    "admin-publications": "",
    "admin-add-entry": "upload a publication",
    "admin-collections": "",
    "admin-categories": "",
    "admin-authors": "",
    "admin-reviews": "",
    "admin-catalogs": "",
    "admin-users": "",
    "admin-add-user": "",
    "admin-access": "rights, API keys",
    "admin-loans": "all loans",
    "admin-jobs": "OCR, AI processing, encryption jobs and failures",
}
_ADMIN_KEYS = ("dashboard", "publications", "collections", "categories", "authors", "reviews", "catalogs", "users")
_ADMIN_KEYS += ("access", "loans", "jobs")
ADMIN_MENU = tuple(f"admin-menu-{key}" for key in _ADMIN_KEYS) + ("menu-admin",)

#: Tours: id → who may take it.
TOURS = {"portal": False, "find-book": False, "account": False, "admin": True}

_ID = re.compile(r"^[a-z0-9-]{1,48}$")


def allowed(staff: bool) -> set:
    ids = set(PLACES) | set(MENU)
    if staff:
        ids |= set(ADMIN_PLACES) | set(ADMIN_MENU)
    return ids


def specification(staff: bool) -> dict:
    """The tool as the model is told about it; the place list depends on whether the reader is staff."""
    places = dict(PLACES)
    if staff:
        places.update(ADMIN_PLACES)
    tours = [tour for tour, admin in TOURS.items() if staff or not admin]
    listing = ", ".join(f"{place} ({what})" if what else place for place, what in places.items())
    return {
        "type": "function",
        "function": {
            "name": SHOW_IN_PORTAL,
            "description": (
                "Highlight places in this portal, for 'where is / how do I / show me' questions or a tour; "
                f"answer in one sentence too. Places: {listing}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "targets": {"type": "array", "items": {"type": "string"}},
                    "tour": {"type": "string", "enum": tours},
                    "auto": {"type": "boolean", "description": "Only if asked to be taken there"},
                },
            },
        },
    }


def navigation(arguments: dict, staff: bool) -> Tuple[Optional[dict], Optional[str], List[str]]:
    """`(navigate frame or None, error for the model or None, rejected ids)` for a `show_in_portal` call."""
    known = allowed(staff)
    raw = arguments.get("targets")
    if isinstance(raw, str):
        raw = [raw]
    requested = [str(item).strip() for item in raw or [] if str(item).strip()]
    targets = list(dict.fromkeys(item for item in requested if item in known))[:MAX_TARGETS]
    rejected = [item for item in requested if item not in known]

    tour = str(arguments.get("tour") or "").strip() or None
    if tour is not None and (tour not in TOURS or (TOURS[tour] and not staff)):
        rejected.append(tour)
        tour = None

    if not targets and tour is None:
        return None, "No known place. Use place ids from the tool's list (e.g. 'loans'), or a tour.", rejected
    frame: dict = {"targets": targets}
    if tour is not None:
        frame["tour"] = tour
    if arguments.get("auto") is True:
        frame["auto"] = True
    error = None
    if rejected:
        error = f"Unknown place ids, not shown: {', '.join(rejected)}."
    return frame, error, rejected


#: A portal path as the reader's browser has it: no query, no fragment, nothing but path characters.
_PATH = re.compile(r"^/[A-Za-z0-9/_\-.]{0,199}$")


def clean_path(value) -> Optional[str]:
    """The portal path the client sent, if it looks like one; anything else is dropped (it reaches the prompt)."""
    value = str(value or "").strip()
    if not value:
        return None
    value = value.split("?", 1)[0].split("#", 1)[0]
    return value if _PATH.match(value) else None


def is_staff(user) -> bool:
    return bool(getattr(user, "is_authenticated", False) and getattr(user, "is_superuser", False))
