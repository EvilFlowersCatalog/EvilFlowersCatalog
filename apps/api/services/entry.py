import mimetypes
import uuid

from apps.api.forms.entries import EntryForm
from apps.core.models import Catalog, User, Entry, Author, Category, Acquisition, Price


class EntryService:
    def __init__(self, catalog: Catalog, creator: User):
        self._catalog = catalog
        self._creator = creator

    def fill(self, entry: Entry, form: EntryForm) -> Entry:
        form.fill(entry)

        if 'author' in form.cleaned_data.keys():
            author, created = Author.objects.get_or_create(
                catalog=self._catalog,
                name=form.cleaned_data['author']['name'],
                surname=form.cleaned_data['author']['surname']
            )
            entry.author = author

        if 'category' in form.cleaned_data.keys():
            category, created = Category.objects.get_or_create(
                creator=self._creator,
                catalog=self._catalog,
                term=form.cleaned_data['category']['term']
            )
            if created:
                category.label = form.cleaned_data['category'].get('label')
                category.scheme = form.cleaned_data['category'].get('scheme')
                category.save()
            entry.category = category

        entry.save()

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

        return entry
