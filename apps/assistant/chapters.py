"""Chapter summaries (IP-024 §7, #119).

"What does this book cover?" and "which chapter explains X?" otherwise cost page reads or rest on the catalogue
summary alone. Each top-level chapter of `Entry.table_of_contents` gets an 80-120 word summary (monad-knowledge
uses 150-200 for embedding; here every word is replayed to a CPU model), generated once from the chapter's text and
kept while the text and the prompt stay the same (`ChapterSummary.digest`).

They reach the model through `get_entry` rather than a tool of their own, which would grow the prompt prefix every
chat pays for (#114): once a publication has summaries, its outline in `get_entry` is the list of chapters, each
with its summary, marked as generated and unchecked (`outline_for_model`).

On CPU a chapter costs a prompt evaluation of up to ~2.5K tokens (the text is cut at `MAX_CHAPTER_CHARS`), about
a minute, so nothing runs automatically: `manage.py summarise_chapters` (or the Celery task) fills them in, a
publication or a batch at a time. Publications lent through Readium LCP are skipped: their text never reaches the
model (IP-018 Q5), and a summary would carry it to every reader who can see the record.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from django.conf import settings

from apps.assistant import prompts, structured

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = prompts.render("chapter_summary")

SUMMARY_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}

#: Characters of a chapter given to the model (monad-knowledge's chapter summaries use 8000).
MAX_CHAPTER_CHARS = 8000

#: Chapters summarised per publication at most; a generated outline can list every section.
MAX_CHAPTERS = 40

#: Text shorter than this is not worth a model call (a part title page, an empty section).
MIN_CHAPTER_CHARS = 300


@dataclass
class Chapter:
    title: str
    page: int
    end_page: int


def chapters(table_of_contents: Optional[List[dict]], page_count: Optional[int]) -> List[Chapter]:
    """The top-level chapters of an outline, each running until the next one starts.

    When the outline has a single top-level heading (the book title), its children are the chapters.
    """
    headings = [heading for heading in (table_of_contents or []) if isinstance(heading.get("page"), int)]
    if not headings:
        return []
    top = min(heading.get("level") or 1 for heading in headings)
    level = top
    if sum(1 for heading in headings if (heading.get("level") or 1) == top) == 1:
        level = top + 1
    picked = [heading for heading in headings if (heading.get("level") or 1) == level] or [
        heading for heading in headings if (heading.get("level") or 1) == top
    ]
    picked = sorted(picked, key=lambda heading: heading["page"])[:MAX_CHAPTERS]

    last = page_count or picked[-1]["page"]
    result = []
    for index, heading in enumerate(picked):
        following = picked[index + 1]["page"] - 1 if index + 1 < len(picked) else last
        result.append(Chapter(str(heading.get("title") or ""), heading["page"], max(heading["page"], following)))
    return result


def chapter_texts(acquisition, wanted: List[Chapter]) -> Dict[int, str]:
    """Text of each chapter, by start page, read from the stored PDF in the reader's page numbering."""
    import fitz

    from apps.assistant.pdf import reader_source_pages

    with acquisition.content.open("rb") as file:
        document = fitz.open(stream=file.read(), filetype="pdf")
    texts = {}
    with document:
        sources = reader_source_pages(len(document), None, acquisition.mime)
        for chapter in wanted:
            parts, size = [], 0
            for reader_page in range(chapter.page, chapter.end_page + 1):
                if reader_page < 1 or reader_page > len(sources) or sources[reader_page - 1] is None:
                    continue
                text = " ".join(document[sources[reader_page - 1] - 1].get_text().split())
                parts.append(text)
                size += len(text)
                if size >= MAX_CHAPTER_CHARS:
                    break
            texts[chapter.page] = " ".join(parts)[:MAX_CHAPTER_CHARS]
    return texts


def digest(text: str) -> str:
    return hashlib.sha256(f"{SYSTEM_PROMPT}\x00{text}".encode("utf-8")).hexdigest()


def summarise(client, chapter: Chapter, text: str) -> tuple:
    """`(summary or None, tokens)` for one chapter."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f'Chapter: "{chapter.title}"\n\nText:\n"""\n{text}\n"""'},
    ]
    content, tokens = "", 0
    for delta in client.chat(messages, tools=None, response_format=SUMMARY_SCHEMA):
        content += delta.content
        if delta.done:
            tokens = delta.total_tokens
    data = structured.parse_object(content, lambda value: isinstance(value.get("summary"), str))
    summary = " ".join(data["summary"].split()) if data else ""
    return (summary or None), tokens


def summarise_entry(entry_id, client=None, force: bool = False) -> dict:
    """Summarise the chapters of one publication that have no current summary. Returns counts."""
    from apps.assistant.models import ChapterSummary
    from apps.core.models import Entry
    from apps.core.services.table_of_contents import source_acquisition

    report = {"chapters": 0, "summarised": 0, "kept": 0, "skipped": 0, "failed": 0}
    entry = Entry.objects.filter(pk=entry_id).first()
    if entry is None:
        return report
    if entry.read_config("readium_enabled"):
        # Never summarised; and a summary from before the publication was licensed must go.
        ChapterSummary.objects.filter(entry=entry).delete()
        report["skipped"] = 1
        return report

    wanted = chapters(entry.table_of_contents, entry.page_count)
    report["chapters"] = len(wanted)
    acquisition = source_acquisition(entry) if wanted else None
    if acquisition is None:
        return report

    try:
        texts = chapter_texts(acquisition, wanted)
    except Exception:
        logger.exception("Could not read the chapters of entry %s", entry.pk)
        report["failed"] = len(wanted)
        return report

    if client is None:
        from apps.assistant.llm import OllamaClient

        client = OllamaClient()
    model = settings.EVILFLOWERS_ASSISTANT_OLLAMA_MODEL or ""
    existing = {(row.page, row.title): row for row in ChapterSummary.objects.filter(entry=entry)}
    keep = set()

    for chapter in wanted:
        text = texts.get(chapter.page) or ""
        key = (chapter.page, chapter.title)
        if len(text) < MIN_CHAPTER_CHARS:
            continue
        keep.add(key)
        current = existing.get(key)
        text_digest = digest(text)
        if current is not None and current.digest == text_digest and not force:
            report["kept"] += 1
            continue
        try:
            summary, tokens = summarise(client, chapter, text)
        except Exception:
            logger.exception("Chapter summary failed: entry %s page %s", entry.pk, chapter.page)
            summary, tokens = None, 0
        if not summary:
            report["failed"] += 1
            continue
        ChapterSummary.objects.update_or_create(
            entry=entry,
            page=chapter.page,
            title=chapter.title,
            defaults={
                "end_page": chapter.end_page,
                "summary": summary,
                "digest": text_digest,
                "model": model,
                "tokens_used": tokens,
            },
        )
        report["summarised"] += 1

    # Chapters that left the outline take their summaries with them.
    stale = [row.pk for key, row in existing.items() if key not in keep]
    if stale:
        ChapterSummary.objects.filter(pk__in=stale).delete()
    return report


#: Chapters with summaries shown in one `get_entry` result (≈ 150 tokens each).
MAX_CHAPTERS_SHOWN = 20

#: Said once with the summaries, so the model treats them as orientation, not as the book's words.
SUMMARY_NOTE = "Chapter summaries are generated by the assistant from the text and unchecked; cite pages, not them."


def outline_for_model(entry_id, table_of_contents: Optional[List[dict]]) -> dict:
    """What `get_entry` shows of the outline: `{"table_of_contents": …}` or `{"chapters": …, "note": …}`.

    Without summaries the outline is unchanged. With them it is the summarised chapters only — title, pages and
    summary — so the result stays small; the sub-headings are what `read_pdf_page` is for.
    """
    from apps.assistant.models import ChapterSummary

    rows = list(
        ChapterSummary.objects.filter(entry_id=entry_id)
        .order_by("page")
        .values("title", "page", "end_page", "summary")[:MAX_CHAPTERS_SHOWN]
    )
    if not rows:
        return {"table_of_contents": table_of_contents} if table_of_contents else {}
    return {"chapters": rows, "chapters_note": SUMMARY_NOTE}
