"""What a reader attaches to a chat message in the viewer.

The viewer sends its `IChatContext` objects as they are. They are stored on the
message (`ChatMessage.contexts`), replayed to the model and returned to every
client that reads the chat back, so only known kinds and fields are kept, every
text is clipped, and nothing that only makes sense for one session — an
authorized crop URL, say — is stored.

Kinds, as the viewer names them:

- `selection`: text the reader selected, and its page;
- `explain`: an explanation the reader got for a selection or a region;
- `region`: a figure, table or equation (IP-018 regions);
- `highlight`: one sentence of an agent annotation.
"""

from typing import Any, List, Optional

#: Attachments kept per message; the viewer de-duplicates, this only bounds abuse.
MAX_CONTEXTS = 10

#: Characters kept of a selection, a highlight or a caption.
MAX_TEXT = 4000

#: Characters kept of an explanation or a table's markdown.
MAX_LONG_TEXT = 8000

REGION_KINDS = ("figure", "table", "equation")


class InvalidContext(ValueError):
    pass


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _optional_text(value: Any, limit: int = MAX_TEXT) -> Optional[str]:
    text = _text(value, limit)
    return text or None


def _page(value: Any) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError):
        raise InvalidContext("`page` must be a page number")
    if page < 1:
        raise InvalidContext("`page` must be 1 or more")
    return page


def _box(value: Any) -> dict:
    if not isinstance(value, dict):
        raise InvalidContext("a box must be an object with x0, y0, x1 and y1")
    try:
        return {key: min(max(float(value[key]), 0.0), 1.0) for key in ("x0", "y0", "x1", "y1")}
    except (KeyError, TypeError, ValueError):
        raise InvalidContext("a box must be an object with x0, y0, x1 and y1")


def _region(value: Any) -> dict:
    if not isinstance(value, dict) or value.get("kind") not in REGION_KINDS:
        raise InvalidContext(f"`region.kind` must be one of {', '.join(REGION_KINDS)}")
    region = {
        "id": _text(value.get("id"), 64),
        "kind": value["kind"],
        "page": _page(value.get("page")),
        "bbox": _box(value.get("bbox")),
        "caption": _optional_text(value.get("caption")),
        "category": _optional_text(value.get("category"), 50),
        "markdown": _optional_text(value.get("markdown"), MAX_LONG_TEXT),
        "latex": _optional_text(value.get("latex")),
    }
    if not region["id"]:
        raise InvalidContext("`region.id` is required")
    return region


def _highlight(value: Any) -> dict:
    if not isinstance(value, dict):
        raise InvalidContext("`highlight` must be an object")
    quads = value.get("quads") or []
    if not isinstance(quads, list):
        raise InvalidContext("`highlight.quads` must be a list")
    highlight = {
        "id": _text(value.get("id"), 64),
        "seq": int(value.get("seq") or 0),
        "page": _page(value.get("page")),
        "quads": [_box(quad) for quad in quads[:50]],
        "text": _text(value.get("text")),
        "note": _optional_text(value.get("note")),
    }
    if not highlight["id"]:
        raise InvalidContext("`highlight.id` is required")
    return highlight


def _one(value: Any) -> dict:
    if not isinstance(value, dict):
        raise InvalidContext("a context must be an object")

    kind = value.get("kind")
    if kind == "selection":
        text = _text(value.get("text"))
        if not text:
            raise InvalidContext("`text` is required")
        return {"kind": kind, "text": text, "page": _page(value.get("page"))}

    if kind == "explain":
        text = _text(value.get("text"))
        if not text:
            raise InvalidContext("`text` is required")
        context = {
            "kind": kind,
            "text": text,
            "page": _page(value.get("page")),
            "explanation": _text(value.get("explanation"), MAX_LONG_TEXT),
        }
        region_id = _optional_text(value.get("regionId"), 64)
        if region_id:
            context["regionId"] = region_id
        return context

    if kind == "region":
        return {"kind": kind, "region": _region(value.get("region"))}

    if kind == "highlight":
        annotation_id = _text(value.get("annotationId"), 64)
        if not annotation_id:
            raise InvalidContext("`annotationId` is required")
        return {"kind": kind, "annotationId": annotation_id, "highlight": _highlight(value.get("highlight"))}

    raise InvalidContext("`kind` must be selection, explain, region or highlight")


def normalize(value: Any) -> Optional[List[dict]]:
    """The contexts as stored, or None when there are none. Raises `InvalidContext`."""
    if value in (None, "", []):
        return None
    if not isinstance(value, list):
        raise InvalidContext("`contexts` must be a list")
    if len(value) > MAX_CONTEXTS:
        raise InvalidContext(f"at most {MAX_CONTEXTS} contexts can be attached to a message")
    return [_one(item) for item in value]


def page_of(context: dict) -> Optional[int]:
    if context["kind"] == "region":
        return context["region"]["page"]
    if context["kind"] == "highlight":
        return context["highlight"]["page"]
    return context.get("page")


def describe(context: dict) -> str:
    """One line telling the model what the reader attached. Deterministic, so the replayed prefix stays stable."""
    kind = context["kind"]
    page = page_of(context)

    if kind == "selection":
        return f'Selected text on page {page}: """{context["text"]}"""'

    if kind == "explain":
        line = f'An explanation the reader got for """{context["text"]}""" on page {page}'
        if context.get("regionId"):
            line += f" (region {context['regionId']})"
        return line + f': """{context["explanation"]}"""'

    if kind == "region":
        region = context["region"]
        line = f"The {region['kind']} {region['id']} on page {page}"
        if region.get("caption"):
            line += f', captioned "{region["caption"]}"'
        if region.get("markdown"):
            line += f":\n{region['markdown']}"
        elif region.get("latex"):
            line += f": {region['latex']}"
        return line

    highlight = context["highlight"]
    line = f'Highlight #{highlight["seq"]} on page {page}: """{highlight["text"]}"""'
    if highlight.get("note"):
        line += f" (note: {highlight['note']})"
    return line
