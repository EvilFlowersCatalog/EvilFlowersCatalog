import mimetypes
import uuid
from functools import reduce
from io import BytesIO
from operator import or_

import isbnlib
from PIL import Image, ImageOps
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
                entry.image_mime = form.cleaned_data["image"].content_type

                entry.image.save(
                    f"cover{mimetypes.guess_extension(entry.image_mime)}",
                    form.cleaned_data["image"],
                )

                buffer, thumbnail_mime = build_thumbnail(
                    form.cleaned_data["image"].image, settings.EVILFLOWERS_IMAGE_THUMBNAIL
                )
                entry.thumbnail_mime = thumbnail_mime

                entry.thumbnail.save(
                    f"thumbnail{mimetypes.guess_extension(thumbnail_mime)}",
                    File(buffer),
                )

        return entry
