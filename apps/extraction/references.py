"""Cross-references from paragraphs to figures, tables, equations and sections (IP-030 §4).

A deterministic pass over one extraction run. It finds "Figure 3.2", "Fig. 3", "Obr. 2.1", "obrázku 4",
"Table 4", "Tab. 1", "tabuľke 2", "Eq. (5)", "rovnica (3)", "Section 2.3", "kapitole 4" in paragraph text, and
matches the number against the printed captions of the same run's regions (or, for a section, the paragraph
whose heading starts with that number). Every match is stored; one whose target is not found is stored with no
target and counted, so the pattern list can grow from the counts (`provenance["references"]`).

It runs after a run becomes active (`apps.extraction.tasks.post_process_run`) and as a backfill
(`manage.py link_extraction`); it needs no service change.
"""

import logging
import re
from typing import Dict, List, Optional

from django.db import transaction

from apps.extraction.models import ExtractionRun, Paragraph, Reference, Region

logger = logging.getLogger(__name__)

_NUMBER = r"(?P<number>\d+(?:\s?[.\-–]\s?\d+)*[a-z]?)"

#: Words that introduce a reference, per kind (English, Slovak, Czech, German), case-insensitive.
WORDS = {
    Reference.Kind.FIGURE: r"fig(?:ure|ures|s)?\.?|obr(?:\.|ázok|ázku|ázkoch|ázkom|ázky|ázek)?|abb(?:\.|ildung)",
    Reference.Kind.TABLE: r"tab(?:le|les|\.|uľka|uľke|uľky|uľku|ulka|ulce|ulky|elle)?",
    Reference.Kind.EQUATION: r"eq(?:uation|s)?\.?|rovnic(?:a|e|i|u)|vzťah(?:u|om)?|vzorec|vzorca|gleichung",
    Reference.Kind.SECTION: r"sec(?:tion|t)?\.?|§|kap(?:itola|itole|itoly|itolu|\.)|oddiel(?:e|u)?|kapitel",
}

#: An equation number is usually in parentheses.
_PATTERNS = {
    kind: re.compile(
        rf"(?<![\w])(?P<anchor>(?:{words})\s*(?:\(\s*{_NUMBER}\s*\)|{_NUMBER.replace('number', 'number2')}))",
        re.IGNORECASE | re.UNICODE,
    )
    for kind, words in WORDS.items()
}

#: The number a printed caption starts with ("Obr. 2.1: …", "Table 4 – …", "(5)").
_CAPTION = {
    kind: re.compile(rf"^\s*(?:{words})\s*\(?\s*{_NUMBER}", re.IGNORECASE | re.UNICODE)
    for kind, words in WORDS.items()
    if kind != Reference.Kind.SECTION
}
_EQUATION_NUMBER = re.compile(rf"\(\s*{_NUMBER}\s*\)\s*$")
_HEADING_NUMBER = re.compile(rf"^\s*{_NUMBER}[.)]?\s")

#: A paragraph this short that starts with the reference is the caption itself, not a reference to it.
CAPTION_CHARS = 300

_REGION_KIND = {
    Reference.Kind.FIGURE: Region.Kind.FIGURE,
    Reference.Kind.TABLE: Region.Kind.TABLE,
    Reference.Kind.EQUATION: Region.Kind.EQUATION,
}


def normalise(number: str) -> str:
    """'2 . 1', '2-1', '2–1.' → '2.1'."""
    number = re.sub(r"\s*[.\-–]\s*", ".", (number or "").strip().lower())
    return number.strip(".")


def find(text: str) -> List[dict]:
    """`[{kind, anchor, number, start}]` of the references in `text`, in order."""
    found = []
    for kind, pattern in _PATTERNS.items():
        for match in pattern.finditer(text or ""):
            number = match.group("number") or match.group("number2")
            if not number:
                continue
            found.append(
                {
                    "kind": kind,
                    "anchor": match.group("anchor")[:100],
                    "number": normalise(number),
                    "start": match.start(),
                }
            )
    return sorted(found, key=lambda item: item["start"])


def caption_number(region: Region) -> Optional[str]:
    """The number of a region by its printed caption (or caption), if it has one."""
    caption = region.printed_caption or region.caption or ""
    kinds = [kind for kind, region_kind in _REGION_KIND.items() if region_kind == region.kind]
    for kind in kinds:
        match = _CAPTION[kind].match(caption)
        if match:
            return normalise(match.group("number"))
    if region.kind == Region.Kind.EQUATION:
        match = _EQUATION_NUMBER.search(caption or region.latex or "")
        if match:
            return normalise(match.group("number"))
    return None


def build(run: ExtractionRun) -> Dict[str, int]:
    """Replace the run's references; returns and records the counts."""
    targets: Dict[tuple, str] = {}
    for region in run.regions.all():
        number = caption_number(region)
        if number:
            targets.setdefault((region.kind, number), region.pk)

    sections: Dict[str, object] = {}
    for paragraph_id, section in (
        run.paragraphs.exclude(section=None).order_by("seq").values_list("pk", "section").iterator()
    ):
        match = _HEADING_NUMBER.match(section or "")
        if match:
            sections.setdefault(normalise(match.group("number")), paragraph_id)

    rows: List[Reference] = []
    counts = {"found": 0, "resolved": 0, "unresolved": 0}
    for paragraph_id, text in run.paragraphs.order_by("seq").values_list("pk", "text").iterator():
        for reference in find(text):
            if reference["start"] == 0 and len(text) <= CAPTION_CHARS and reference["kind"] != Reference.Kind.SECTION:
                continue
            region_id = target_paragraph_id = None
            if reference["kind"] == Reference.Kind.SECTION:
                target_paragraph_id = sections.get(reference["number"])
            else:
                region_id = targets.get((_REGION_KIND[reference["kind"]], reference["number"]))
            resolved = bool(region_id or target_paragraph_id)
            counts["found"] += 1
            counts["resolved" if resolved else "unresolved"] += 1
            by_kind = f"{reference['kind']}_{'resolved' if resolved else 'unresolved'}"
            counts[by_kind] = counts.get(by_kind, 0) + 1
            rows.append(
                Reference(
                    run=run,
                    paragraph_id=paragraph_id,
                    region_id=region_id,
                    target_paragraph_id=target_paragraph_id,
                    kind=reference["kind"],
                    anchor=reference["anchor"],
                    number=reference["number"][:30],
                )
            )

    with transaction.atomic():
        Reference.objects.filter(run=run).delete()
        Reference.objects.bulk_create(rows, batch_size=2000)
        provenance = dict(ExtractionRun.objects.filter(pk=run.pk).values_list("provenance", flat=True).first() or {})
        provenance["references"] = counts
        ExtractionRun.objects.filter(pk=run.pk).update(provenance=provenance)
    logger.info("References of run %s: %s", run.pk, counts)
    return counts


def referring_paragraphs(region_ids) -> Dict[str, Paragraph]:
    """The first paragraph (in reading order) that refers to each region."""
    first: Dict[str, Paragraph] = {}
    references = (
        Reference.objects.filter(region_id__in=list(region_ids)).select_related("paragraph").order_by("paragraph__seq")
    )
    for reference in references:
        first.setdefault(str(reference.region_id), reference.paragraph)
    return first
