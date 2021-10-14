import mimetypes
import uuid
from io import BytesIO

from django.conf import settings
from django.core.files import File

from apps.api.forms.entries import EntryForm
from apps.core.models import Catalog, User, Entry, Author, Category, Acquisition, Price


class EntryService:
    def __init__(self, catalog: Catalog, creator: User):
        self._catalog = catalog
        self._creator = creator

    def populate(self, entry: Entry, form: EntryForm) -> Entry:
        form.populate(entry)

        if 'author' in form.cleaned_data.keys():
            author, created = Author.objects.get_or_create(
                catalog=self._catalog,
                name=form.cleaned_data['author']['name'],
                surname=form.cleaned_data['author']['surname']
            )
            entry.author = author

        entry.save()

        if 'categories' in form.cleaned_data.keys():
            entry.categories.clear()
            for record in form.cleaned_data.get('categories', []):
                category, created = Category.objects.get_or_create(
                    creator=self._creator,
                    catalog=self._catalog,
                    term=record['term']
                )
                if created:
                    category.label = record.get('label')
                    category.scheme = record.get('scheme')
                    category.save()
                entry.categories.add(category)

        if 'category_ids' in form.cleaned_data.keys():
            entry.contributors.clear()
            for contributor in form.cleaned_data.get('category_ids', []):
                entry.categories.add(contributor)

        for record in form.cleaned_data.get('acquisitions', []):
            acquisition = Acquisition(
                entry=entry,
                relation=record.get('relation'),
                mime=record['content'].content_type
            )

            if 'content' in record.keys():
                acquisition.content.save(
                    f"{uuid.uuid4()}{mimetypes.guess_extension(acquisition.mime)}",
                    record['content']
                )

            for price in record.get('prices', []):
                Price.objects.create(
                    acquisition=acquisition,
                    currency=price['currency_code'],
                    value=price['value']
                )

        if 'contributors' in form.cleaned_data:
            entry.contributors.clear()
            for record in form.cleaned_data.get('contributors', []):
                contributor, is_created = Author.objects.get_or_create(
                    catalog=self._catalog,
                    name=record['name'],
                    surname=record['surname']
                )
                entry.contributors.add(contributor)

        if 'contributor_ids' in form.cleaned_data.keys():
            entry.contributors.clear()
            for contributor in form.cleaned_data.get('contributor_ids', []):
                entry.contributors.add(contributor)

        if 'feeds' in form.cleaned_data:
            entry.feeds.clear()
            for feed in form.cleaned_data.get('feeds', []):
                entry.feeds.add(feed)

        if 'image' in form.cleaned_data:
            if form.cleaned_data['image'] is None:
                entry.image = None
                entry.image_mime = None
                entry.thumbnail = None
            else:
                entry.image_mime = form.cleaned_data['image'].content_type

                entry.image.save(
                    f"cover{mimetypes.guess_extension(entry.image_mime)}",
                    form.cleaned_data['image']
                )

                buffer = BytesIO()
                thumbnail = form.cleaned_data['image'].image.copy()
                thumbnail.thumbnail(settings.OPDS['IMAGE_THUMBNAIL'])
                thumbnail.save(buffer, format=form.cleaned_data['image'].image.format)
                entry.thumbnail.save(
                    f"thumbnail{mimetypes.guess_extension(entry.image_mime)}",
                    File(buffer)
                )

        return entry
