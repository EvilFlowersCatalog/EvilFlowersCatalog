"""`annotate_figure`: marks (boxes, ellipses, arrows, points) drawn on a cited figure (#179).

The chat model cannot see pixels, so the work is split. The chat model decides *what* to point at and *why*
(`targets: [{hint, label, note}]`). A grounding request finds *where*: the figure's crop goes to the vision model
(`follow.describe`'s route, on its own request), which answers with positions on a 0-1000 scale.

The marks ride on the figure's citation (elvira-portal `docs/assistant-figure-marks.md`). They are set on this
turn's ledger source of the label, so every citation of it from here on carries them: streamed `citation` frames,
the closing `citations` frame and the stored `citations` column. A citation already sent this turn is sent again
with the marks; the portal merges by label. The tool's own row keeps no `evidence`, so the next turn's ledger
starts without marks.

Grounding is the vision model only. Two other routes in the issue are not built:
- PDF text-layer word boxes: they are only valid if the crop is exactly `Region.bbox` of the page, and the crop is
  cut by the extraction services. That is not verified.
- Table cells and equation terms: extraction keeps no cell or term geometry.

Gates: an F# label this turn may cite (#162), a figure whose crop the reader may see (`citations._region` gives
`image_url` on the same check), vision on, and `EVILFLOWERS_ASSISTANT_FIGURE_MARKS` on (the tool costs ~150
tokens of the first round's prefix, see `test_tool_surface`).
"""

import base64
import json
import logging
import re
from typing import List, Optional

from django.conf import settings

from apps.mcp.errors import ToolError

logger = logging.getLogger(__name__)

NAME = "annotate_figure"
#: Marks one call may ask for (the portal draws at most 12).
MAX_TARGETS = 6
LABEL_CHARS = 60
NOTE_CHARS = 280
HINT_CHARS = 200
SHAPES = ("box", "ellipse", "arrow", "point")
#: The grounding model's scale: Qwen-VL's relative coordinates.
SCALE = 1000
#: Smallest side of a box, as a fraction of the crop: anything thinner was not found.
MIN_SIDE = 0.01

SPECIFICATION = (
    f"Draw marks on a figure (F#) you show, to point at its parts: for each, what to point at (hint), a short "
    f"label and a one-sentence note. At most {MAX_TARGETS}.",
    {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "targets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "hint": {"type": "string"},
                        "label": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["hint", "label"],
                },
            },
        },
        "required": ["label", "targets"],
    },
)


def offered() -> bool:
    from apps.assistant.follow import vision_enabled

    return bool(getattr(settings, "EVILFLOWERS_ASSISTANT_FIGURE_MARKS", False)) and vision_enabled()


def _clip(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _targets(arguments: dict) -> List[dict]:
    raw = arguments.get("targets")
    if not isinstance(raw, list) or not raw:
        raise ToolError("`targets` must be a list of {hint, label, note}.")
    targets = []
    for item in raw[:MAX_TARGETS]:
        if not isinstance(item, dict):
            continue
        hint = _clip(item.get("hint"), HINT_CHARS)
        label = _clip(item.get("label"), LABEL_CHARS)
        if not hint or not label:
            continue
        target = {"hint": hint, "label": label}
        note = _clip(item.get("note"), NOTE_CHARS)
        if note:
            target["note"] = note
        targets.append(target)
    if not targets:
        raise ToolError("Every target needs a `hint` (what to point at) and a `label`.")
    return targets


def annotate(context, arguments: dict) -> dict:
    from apps.assistant import citations
    from apps.assistant.evidence import normalise
    from apps.extraction.models import Region

    label = normalise(str(arguments.get("label") or ""))
    ledger = context.ledger
    if label is None or not label.startswith("F") or ledger is None or not ledger.citable(label):
        raise ToolError("`label` must be an F# figure label found for this question.")
    source = ledger.sources[label]
    if not source.get("region_id"):
        raise ToolError("`label` must be an F# figure label found for this question.")
    targets = _targets(arguments)
    if not offered():
        raise ToolError("Marks cannot be drawn here; explain the figure in words.")
    if not citations._access(context.request).readable(source.get("entry_id")):
        raise ToolError("The reader cannot see this figure; explain it in words.")
    region = Region.objects.visible().filter(pk=source["region_id"]).first()
    if region is None or not region.image_path:
        raise ToolError("This figure has no image to draw on; explain it in words.")

    placed = ground(region, targets, book=context.book)
    marks = [mark for mark in placed if mark is not None]
    missing = [target["hint"] for target, mark in zip(targets, placed) if mark is None]
    payload = {"label": label, "marks": marks, "not_found": missing}
    if not marks:
        payload["note"] = "None of the targets could be located; explain the figure in words."
        return payload

    # This turn's citations of the label carry the marks (module docstring).
    ledger.sources[label] = {**source, "marks": marks}
    context.frames.append(("citation", citations.complete(label, ledger.sources[label], "", context.request)))
    payload["note"] = f"Cite [{label}] where you explain it; the marks are shown on it."
    return payload


def ground(region, targets: List[dict], client=None, book=None) -> List[Optional[dict]]:
    """One mark per target, in order; None for a target the model did not place."""
    from apps.assistant import prompts
    from apps.files.storage import get_storage

    try:
        with get_storage().open(region.image_path, "rb") as file:
            image = base64.b64encode(file.read()).decode("ascii")
    except Exception:
        logger.warning("Figure crop %s could not be read for marks", region.pk)
        return [None] * len(targets)

    caption = region.printed_caption or region.caption or ""
    wanted = "\n".join(f"{index}. {target['hint']}" for index, target in enumerate(targets, start=1))
    messages = [
        {"role": "system", "content": prompts.render("grounding")},
        {"role": "user", "content": f"Caption: {caption or '(none)'}\n\nFind:\n{wanted}", "images": [image]},
    ]
    if client is None:
        from apps.assistant.llm import client as make_client

        client = make_client()
    text = ""
    try:
        for delta in client.chat(messages, max_tokens=80 * len(targets) + 40):
            text += delta.content or ""
            if book is not None:
                book(delta)
    except Exception:
        logger.exception("Grounding request for figure %s failed", region.pk)
        return [None] * len(targets)

    found = parse(text, len(targets))
    return [
        (
            {**found[index], "label": target["label"], **({"note": target["note"]} if target.get("note") else {})}
            if index in found
            else None
        )
        for index, target in enumerate(targets)
    ]


_JSON = re.compile(r"[\[{].*[\]}]", re.DOTALL)


def parse(text: str, count: int) -> dict:
    """`{target index (0-based): geometry}` from the grounding model's answer; anything unusable is left out."""
    match = _JSON.search(text or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    items = data.get("marks") if isinstance(data, dict) else data
    found: dict = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("id")) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= index < count or index in found:
            continue
        geometry = sanitise(item)
        if geometry is not None:
            found[index] = geometry
    return found


def _number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN


def sanitise(item: dict) -> Optional[dict]:
    """A known shape with the coordinates it needs, as crop fractions clamped to 0-1, corners ordered.

    Coordinates on the 0-1000 scale asked for; all at most 1 are taken as fractions already. Qwen-VL's own
    `bbox_2d: [x0, y0, x1, y1]` is a box.
    """
    shape = str(item.get("shape") or "box").strip().lower()
    values = {key: item.get(key) for key in ("x0", "y0", "x1", "y1")}
    box = item.get("bbox_2d") or item.get("bbox")
    if isinstance(box, list) and len(box) == 4:
        values = dict(zip(("x0", "y0", "x1", "y1"), box))
    if shape not in SHAPES:
        return None
    needed = ("x0", "y0") if shape == "point" else ("x0", "y0", "x1", "y1")
    numbers = {key: _number(values.get(key)) for key in needed}
    if any(value is None for value in numbers.values()):
        return None
    scale = 1.0 if all(value <= 1.0 for value in numbers.values()) else float(SCALE)
    coords = {key: round(min(max(value / scale, 0.0), 1.0), 4) for key, value in numbers.items()}
    if shape in ("box", "ellipse"):
        x0, x1 = sorted((coords["x0"], coords["x1"]))
        y0, y1 = sorted((coords["y0"], coords["y1"]))
        if x1 - x0 < MIN_SIDE or y1 - y0 < MIN_SIDE:
            return None
        coords = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
    elif shape == "arrow" and (coords["x0"], coords["y0"]) == (coords["x1"], coords["y1"]):
        return None
    return {"shape": shape, **coords}


def slim(payload: dict) -> dict:
    """What the chat model sees: which marks were drawn and which were not, no coordinates."""
    return {
        "label": payload.get("label"),
        "drawn": [mark.get("label") for mark in payload.get("marks") or []],
        "not_found": payload.get("not_found") or [],
        "note": payload.get("note"),
    }


__all__ = ["NAME", "SPECIFICATION", "annotate", "ground", "offered", "parse", "sanitise", "slim"]
