import mimetypes
import uuid
from functools import reduce
from io import BytesIO
from operator import or_

import isbnlib
from django.conf import settings
from django.core.files import File
from django.db.models import Q
from isbnlib.registry import bibformatters

from apps.api.forms.entries import EntryForm
from apps.core.models import Catalog, User, Entry, Author, Category, Acquisition, Price, EntryAuthor


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
        conditions = [Q(title=entry.title)]
        if entry.identifiers.get("isbn"):
            conditions.append(Q(identifiers__isbn=entry.identifiers.get("isbn")))
        if entry.identifiers.get("doi"):
            conditions.append(Q(identifiers__doi=entry.identifiers.get("doi")))
        if Entry.objects.exclude(pk=entry.pk).filter(catalog=self._catalog).filter(reduce(or_, conditions)).exists():
            raise self.AlreadyExists()

        # TODO: implement these meta downloaders for real
        if all(
            [
                entry.citation is None,
                (entry.identifiers and entry.identifiers.get("isbn")),
                entry.read_config("evilflowers_metadata_fetch"),
            ]
        ):
            metadata = isbnlib.meta(entry.identifiers["isbn"])
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
            else:
                entry.image_mime = form.cleaned_data["image"].content_type

                entry.image.save(
                    f"cover{mimetypes.guess_extension(entry.image_mime)}",
                    form.cleaned_data["image"],
                )

                buffer = BytesIO()
                thumbnail = form.cleaned_data["image"].image.copy()
                thumbnail.thumbnail(settings.EVILFLOWERS_IMAGE_THUMBNAIL)
                thumbnail.save(buffer, format=form.cleaned_data["image"].image.format)
                buffer.seek(0)

                entry.thumbnail.save(
                    f"thumbnail{mimetypes.guess_extension(entry.image_mime)}",
                    File(buffer),
                )

        return entry
