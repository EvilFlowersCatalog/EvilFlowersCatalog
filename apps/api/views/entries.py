import mimetypes
import uuid
from http import HTTPStatus

from django.views.generic.base import View

from apps.api.errors import ValidationException
from apps.api.filters.entries import EntryFilter
from apps.api.forms.entries import EntryForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.entries import EntrySerializer
from apps.core.models import Entry, Author, Category, Acquisition, Price


class EntryManagement(View):
    require_apikey = ['post', 'get']

    def post(self, request):
        form = EntryForm.create_from_request(request)
        form.fields['catalog_id'].queryset = form.fields['catalog_id'].queryset.filter(creator_id=request.api_key.user)

        if not form.is_valid():
            raise ValidationException(request, form)

        entry = Entry(creator=request.api_key.user)
        form.fill(entry)

        if 'author' in form.cleaned_data.keys():
            author, created = Author.objects.get_or_create(
                catalog=entry.catalog,
                name=form.cleaned_data['author']['name'],
                surname=form.cleaned_data['author']['surname']
            )
            entry.author = author

        if 'category' in form.cleaned_data.keys():
            category, created = Category.objects.get_or_create(
                creator=request.api_key.user,
                catalog=entry.catalog,
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

        return SingleResponse(request, entry, serializer=EntrySerializer.Detailed, status=HTTPStatus.CREATED)

    def get(self, request):
        entries = EntryFilter(request.GET, queryset=Entry.objects.all(), request=request).qs

        return PaginationResponse(
            request, entries, page=request.GET.get('page', 1), serializer=EntrySerializer.Base
        )
