import mimetypes
import uuid
from http import HTTPStatus

from django.db import transaction
from django.http import FileResponse
from django.utils.translation import gettext as _
from django.views.generic.base import View

from apps.api.decorators import require_apikey
from apps.api.errors import ValidationException, ApiException
from apps.api.filters.entries import EntryFilter
from apps.api.forms.entries import CreateEntryForm, UpdateEntryForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.entries import EntrySerializer
from apps.core.models import Entry, Acquisition, Price, Catalog


@require_apikey
def list_entries(request):
    entries = EntryFilter(request.GET, queryset=Entry.objects.all(), request=request).qs

    return PaginationResponse(
        request, entries, page=request.GET.get('page', 1), serializer=EntrySerializer.Base
    )


@require_apikey
@transaction.atomic
def create_entry(request, catalog_id: uuid.UUID) -> SingleResponse:
    try:
        catalog = Catalog.objects.get(pk=catalog_id)
    except Catalog.DoesNotExist as e:
        raise ApiException(request, _("Catalog not found"), status_code=HTTPStatus.NOT_FOUND, previous=e)

    if catalog.creator != request.api_key.user:
        raise ApiException(request, _("Insufficient permissions"), status_code=HTTPStatus.FORBIDDEN)

    form = CreateEntryForm.create_from_request(request)
    form.fields['category_id'].queryset = form.fields['category_id'].queryset.filter(catalog=catalog)
    form.fields['author_id'].queryset = form.fields['author_id'].queryset.filter(catalog=catalog)

    if not form.is_valid():
        raise ValidationException(request, form)

    entry = Entry(creator=request.api_key.user, catalog=catalog)
    entry.fill(form, request.api_key.user)
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


class EntryDetail(View):
    require_apikey = ['put', 'get', 'delete']

    @staticmethod
    def _get_entry(request, catalog_id: uuid.UUID, entry_id: uuid.UUID) -> Entry:
        try:
            entry = Entry.objects.get(pk=entry_id, catalog_id=catalog_id)
        except Entry.DoesNotExist:
            raise ApiException(request, _("Entry not found"), status_code=HTTPStatus.NOT_FOUND)

        if not request.api_key.user.is_superuser and entry.creator_id != request.api_key.user_id:
            raise ApiException(request, _("Insufficient permissions"), status_code=HTTPStatus.FORBIDDEN)

        return entry

    def get(self, request, catalog_id: uuid.UUID, entry_id: uuid.UUID):
        entry = self._get_entry(request, catalog_id, entry_id)

        return SingleResponse(request, entry, serializer=EntrySerializer.Detailed)

    def put(self, request, catalog_id: uuid.UUID, entry_id: uuid.UUID):
        entry = self._get_entry(request, catalog_id, entry_id)

        form = UpdateEntryForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        entry.fill(form, request.api_key.user)
        entry.save()

        return SingleResponse(request, entry, serializer=EntrySerializer.Detailed)

    def delete(self, request, catalog_id: uuid.UUID, entry_id: uuid.UUID):
        entry = self._get_entry(request, catalog_id, entry_id)
        entry.hard_delete()

        return SingleResponse(request)


def download(request, acquisition_id: uuid.UUID):
    try:
        acquisition = Acquisition.objects.get(pk=acquisition_id)
    except Acquisition.DoesNotExist:
        raise ApiException(request, _("Acquisition not found"), status_code=HTTPStatus.NOT_FOUND)

    return FileResponse(acquisition.content, as_attachment=True, filename=acquisition.entry.title)
