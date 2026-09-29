import mimetypes
import uuid
from functools import reduce
from io import BytesIO
from operator import or_

import hashlib

import isbnlib
from PIL import Image, ImageDraw, ImageFont, ImageOps
from django.conf import settings
from django.core.files import File
from django.db.models import Q
from isbnlib.registry import bibformatters

from apps.api.forms.entries import EntryForm
from apps.core.models import Catalog, User, Entry, Author, Category, Acquisition, Price, EntryAuthor


def build_thumbnail(source_image: Image.Image, size: tuple[int, int]) -> tuple[BytesIO, str]:
    """Downscale a cover image into a thumbnail buffer.

    Returns the encoded buffer and its MIME type. The caller stores the MIME on
    ``Entry.thumbnail_mime`` and uses it to serve the file, so the thumbnail is
    decoupled from the cover's format (``Entry.image_mime``).

    Covers are re-encoded as JPEG: the thumbnail is a small lossy preview, and
    JPEG is a fraction of the size of a PNG for the photographic covers that
    dominate the catalog, while remaining universally renderable by both the web
    portal and OPDS reader apps. PNG is kept only when the source actually has
    an alpha channel (rare for covers) so transparency is not flattened onto
    black.

    Quality steps over a plain ``Image.thumbnail`` + ``save``:

      * ``ImageOps.exif_transpose`` — honour the orientation tag so covers shot
        on a phone are not stored sideways.
      * ``LANCZOS`` resampling — the highest-quality downscaling filter. Modern
        Pillow defaults ``thumbnail()`` to the softer ``BICUBIC``.
      * format-appropriate encoder flags (``optimize``, ``quality``,
        ``progressive``).
    """
    thumbnail = ImageOps.exif_transpose(source_image.copy())
    thumbnail.thumbnail(size, Image.Resampling.LANCZOS)

    has_alpha = thumbnail.mode in ("RGBA", "LA", "PA") or (thumbnail.mode == "P" and "transparency" in thumbnail.info)

    if has_alpha:
        mime = "image/png"
        save_options: dict = {"format": "PNG", "optimize": True}
    else:
        mime = "image/jpeg"
        # JPEG carries no alpha channel — flatten palette/greyscale-alpha sources.
        if thumbnail.mode not in ("RGB", "L"):
            thumbnail = thumbnail.convert("RGB")
        save_options = {"format": "JPEG", "quality": 82, "optimize": True, "progressive": True}

    buffer = BytesIO()
    thumbnail.save(buffer, **save_options)
    buffer.seek(0)
    return buffer, mime


# Muted, dark background tones for generated placeholders. Kept dark enough that
# the light title text stays legible on every one of them.
PLACEHOLDER_PALETTE = (
    (38, 70, 83),  # deep teal
    (42, 54, 87),  # indigo
    (61, 52, 74),  # plum
    (45, 66, 55),  # forest
    (74, 55, 46),  # umber
    (55, 60, 68),  # slate
    (72, 45, 58),  # wine
    (40, 61, 72),  # steel blue
)


# Pillow's bundled default (Aileron) has no Latin Extended glyphs, so Slovak
# titles and names ("Krivá", "Štatistika") rendered as boxes. DejaVu Sans covers
# them; the image installs `fonts-dejavu-core`. The macOS paths keep local runs
# correct, and the bundled font stays as the last resort.
PLACEHOLDER_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


def _placeholder_font(size: int) -> ImageFont.FreeTypeFont:
    for path in (getattr(settings, "EVILFLOWERS_PLACEHOLDER_FONT", None), *PLACEHOLDER_FONT_CANDIDATES):
        if path:
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Greedy word-wrap ``text`` to lines no wider than ``max_width`` pixels."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if not current or draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def build_placeholder_thumbnail(title: str, subtitle: str, size: tuple[int, int]) -> tuple[BytesIO, str]:
    """Render a clean text placeholder for entries without a usable cover.

    Produces a portrait JPEG with a deterministic muted background (a given entry
    always gets the same colour) and the wrapped title over it, with the author
    beneath. Uses DejaVu Sans when available (see `_placeholder_font`) so
    diacritics render; Pillow's bundled font is only the fallback.
    """
    title = (title or "").strip() or "Untitled"
    subtitle = (subtitle or "").strip()

    # Portrait 2:3 canvas bounded by the configured thumbnail box height.
    max_width, max_height = size
    height = max_height
    width = min(max_width, round(height * 2 / 3))

    background = PLACEHOLDER_PALETTE[
        hashlib.md5(f"{title}|{subtitle}".encode()).digest()[0] % len(PLACEHOLDER_PALETTE)
    ]
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)

    padding = round(width * 0.12)
    text_width = width - 2 * padding
    title_font = _placeholder_font(max(18, round(width * 0.11)))
    subtitle_font = _placeholder_font(max(12, round(width * 0.06)))

    # Wrap the title and cap the number of lines so very long titles do not
    # overflow the cover; the last kept line gets an ellipsis.
    lines = _wrap_text(draw, title, title_font, text_width)
    max_lines = 6
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".") + "…"

    ascent, descent = title_font.getmetrics()
    line_height = ascent + descent
    line_spacing = round(line_height * 0.25)
    block_height = len(lines) * line_height + (len(lines) - 1) * line_spacing

    # Centre the title block, biased slightly upward to leave room for the author.
    y = (height - block_height) // 2 - round(height * 0.04)
    for line in lines:
        line_width = draw.textlength(line, font=title_font)
        draw.text(((width - line_width) / 2, y), line, font=title_font, fill=(245, 245, 245))
        y += line_height + line_spacing

    if subtitle:
        sub_lines = _wrap_text(draw, subtitle, subtitle_font, text_width)[:2]
        sub_ascent, sub_descent = subtitle_font.getmetrics()
        sub_line_height = sub_ascent + sub_descent
        sub_y = height - padding - len(sub_lines) * sub_line_height
        for line in sub_lines:
            line_width = draw.textlength(line, font=subtitle_font)
            draw.text(((width - line_width) / 2, sub_y), line, font=subtitle_font, fill=(200, 200, 200))
            sub_y += sub_line_height

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=82, optimize=True, progressive=True)
    buffer.seek(0)
    return buffer, "image/jpeg"


def set_cover(entry: Entry, uploaded_image, *, mime: str) -> Entry:
    """Store `uploaded_image` as the entry's cover and derive its thumbnail.

    Shared by `EntryService.populate` (REST `image`) and the MCP cover upload
    link, so both produce the same `cover.*` + `thumbnail.*` pair.
    """
    entry.image_mime = mime
    entry.image.save(f"cover{mimetypes.guess_extension(mime)}", uploaded_image)

    uploaded_image.seek(0)
    buffer, thumbnail_mime = build_thumbnail(Image.open(uploaded_image), settings.EVILFLOWERS_IMAGE_THUMBNAIL)
    entry.thumbnail_mime = thumbnail_mime
    entry.thumbnail.save(f"thumbnail{mimetypes.guess_extension(thumbnail_mime)}", File(buffer))
    return entry


def attach_acquisition(entry: Entry, uploaded_file, *, mime: str, relation: str = None) -> Acquisition:
    """Store `uploaded_file` as a new acquisition of `entry`.

    Shared by `POST /api/v1/catalogs/{c}/entries/{e}` and the MCP upload link so
    both paths trigger the same side effects: saving the content fires
    `touch_entry` → `Entry.post_save`, which queues Readium LCP encryption when
    the entry has `readium_enabled`, and a PDF is handed to the text service.
    """
    import logging

    from apps.dataverse.services.text_publish import TextServiceClient

    acquisition = Acquisition(
        entry=entry,
        relation=relation or Acquisition.AcquisitionType.ACQUISITION,
        mime=mime,
    )
    # Saved first so `upload_to_path` has a primary key to work with.
    acquisition.save()
    acquisition.content.save(f"{uuid.uuid4()}{mimetypes.guess_extension(mime)}", uploaded_file)

    if acquisition.mime == Acquisition.AcquisitionMIME.PDF:
        try:
            TextServiceClient().process_acquisition(acquisition.content.name, str(entry.pk))
        except Exception:
            # Text extraction is best-effort; it must never fail the upload.
            logging.getLogger("apps.api.services.entry").exception(
                "Failed to enqueue text processing task for acquisition_id=%s", acquisition.pk
            )

    return acquisition


class EntryService:
    class AlreadyExists(Exception):
        pass

    def __init__(self, catalog: Catalog, creator: User):
        self._catalog = catalog
        self._creator = creator

    def populate(self, entry: Entry, form: EntryForm) -> Entry:
        form.populate(entry)

        # Conflicts
        # TODO: this is suppose to be some kind of a setting
        identifiers = entry.identifiers or {}
        conditions = [Q(title=entry.title)]
        if identifiers.get("isbn"):
            conditions.append(Q(identifiers__isbn=identifiers.get("isbn")))
        if identifiers.get("doi"):
            conditions.append(Q(identifiers__doi=identifiers.get("doi")))
        if Entry.objects.exclude(pk=entry.pk).filter(catalog=self._catalog).filter(reduce(or_, conditions)).exists():
            raise self.AlreadyExists()

        # TODO: implement these meta downloaders for real
        if all(
            [
                entry.citation is None,
                identifiers.get("isbn"),
                entry.read_config("evilflowers_metadata_fetch"),
            ]
        ):
            metadata = isbnlib.meta(identifiers["isbn"])
            if metadata:
                entry.citation = bibformatters["bibtex"](metadata)

        entry.save()

        if "categories" in form.cleaned_data.keys():
            entry.categories.clear()
            for record in form.cleaned_data.get("categories", []):
                category, created = Category.objects.get_or_create(
                    catalog=self._catalog,
                    term=record["term"],
                    defaults={
                        "creator": self._creator,
                        "label": record.get("label"),
                        "scheme": record.get("scheme"),
                    },
                )
                entry.categories.add(category)

        if "category_ids" in form.cleaned_data.keys():
            entry.categories.clear()
            for category in form.cleaned_data.get("category_ids", []):
                entry.categories.add(category)

        for record in form.cleaned_data.get("acquisitions", []):
            acquisition = Acquisition(
                entry=entry,
                relation=record.get("relation"),
                mime=record["content"].content_type,
            )

            if "content" in record.keys():
                acquisition.content.save(
                    f"{uuid.uuid4()}{mimetypes.guess_extension(acquisition.mime)}",
                    record["content"],
                )

            for price in record.get("prices", []):
                Price.objects.create(
                    acquisition=acquisition,
                    currency=price["currency_code"],
                    value=price["value"],
                )

        if "authors" in form.cleaned_data.keys():
            entry.authors.clear()
            for index, item in enumerate(form.cleaned_data.get("authors")):
                author, created = Author.objects.get_or_create(
                    catalog=self._catalog,
                    name=item["name"],
                    surname=item["surname"],
                )
                EntryAuthor.objects.create(entry=entry, author=author, position=index)

        if "author_ids" in form.cleaned_data.keys():
            entry.authors.clear()
            for index, author in enumerate(form.cleaned_data.get("author_ids", [])):
                EntryAuthor.objects.create(entry=entry, author=author, position=index)

        if "feeds" in form.cleaned_data:
            entry.feeds.clear()
            for feed in form.cleaned_data.get("feeds", []):
                entry.feeds.add(feed)

        if "image" in form.cleaned_data:
            if form.cleaned_data["image"] is None:
                entry.image = None
                entry.image_mime = None
                entry.thumbnail = None
                entry.thumbnail_mime = None
            else:
                set_cover(entry, form.cleaned_data["image"], mime=form.cleaned_data["image"].content_type)

        return entry
