import json
import mimetypes
import uuid
from http import HTTPStatus

from django.db import transaction
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _

from apps.core.errors import ValidationException, ProblemDetailException
from apps.api.filters.entries import EntryFilter
from apps.api.forms.entries import EntryForm, AcquisitionMetaForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.entries import EntrySerializer, AcquisitionSerializer
from apps.api.services.entry import EntryService
from apps.core.models import Entry, Acquisition, Price, Catalog
from apps.view.base import SecuredView


class EntryPaginator(SecuredView):
    def get(self, request):
        entries = EntryFilter(request.GET, queryset=Entry.objects.all(), request=request).qs

        return PaginationResponse(request, entries, serializer=EntrySerializer.Base)


class EntryManagement(SecuredView):
    @method_decorator(transaction.atomic)
    def post(self, request, catalog_id: uuid.UUID):
        try:
            catalog = Catalog.objects.get(pk=catalog_id)
        except Catalog.DoesNotExist as e:
            raise ProblemDetailException(request, _("Catalog not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if catalog.creator != request.user:
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        form = EntryForm.create_from_request(request)
        form.fields['category_ids'].queryset = form.fields['category_ids'].queryset.filter(catalog=catalog)
        form.fields['author_id'].queryset = form.fields['author_id'].queryset.filter(catalog=catalog)

        if not form.is_valid():
            raise ValidationException(request, form)

        entry = Entry(creator=request.user, catalog=catalog)
        service = EntryService(catalog, request.user)
        service.populate(entry, form)

        return SingleResponse(request, entry, serializer=EntrySerializer.Detailed, status=HTTPStatus.CREATED)


class EntryDetail(SecuredView):
    @staticmethod
    def _get_entry(request, catalog_id: uuid.UUID, entry_id: uuid.UUID) -> Entry:
        try:
            entry = Entry.objects.get(pk=entry_id, catalog_id=catalog_id)
        except Entry.DoesNotExist:
            raise ProblemDetailException(request, _("Entry not found"), status=HTTPStatus.NOT_FOUND)

        if not request.user.is_superuser and entry.creator_id != request.user_id:
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return entry

    def get(self, request, catalog_id: uuid.UUID, entry_id: uuid.UUID):
        entry = self._get_entry(request, catalog_id, entry_id)

        return SingleResponse(request, entry, serializer=EntrySerializer.Detailed)

    def post(self, request, catalog_id: uuid.UUID, entry_id: uuid.UUID):
        entry = self._get_entry(request, catalog_id, entry_id)

        try:
            metadata = json.loads(request.POST.get('metadata', '{}'))
        except json.JSONDecodeError as e:
            raise ProblemDetailException(
                request,
                title=_('Unable to parse metadata for request file'),
                status=HTTPStatus.BAD_REQUEST,
                previous=e
            )

        form = AcquisitionMetaForm(metadata, request)

        if not form.is_valid():
            raise ValidationException(request, form)

        acquisition = Acquisition(
            entry=entry,
            relation=form.cleaned_data.get('relation', Acquisition.AcquisitionType.ACQUISITION),
            mime=request.FILES['content'].content_type
        )

        if 'content' in request.FILES.keys():
            acquisition.content.save(
                f"{uuid.uuid4()}{mimetypes.guess_extension(acquisition.mime)}",
                request.FILES['content']
            )

        for price in form.cleaned_data.get('prices', []):
            Price.objects.create(
                acquisition=acquisition,
                currency=price['currency_code'],
                value=price['value']
            )

        return SingleResponse(
            request, acquisition, serializer=AcquisitionSerializer.Detailed, status=HTTPStatus.CREATED
        )

    def put(self, request, catalog_id: uuid.UUID, entry_id: uuid.UUID):
        entry = self._get_entry(request, catalog_id, entry_id)

        form = EntryForm.create_from_request(request)
        form.fields['category_ids'].queryset = form.fields['category_ids'].queryset.filter(catalog_id=catalog_id)
        form.fields['author_id'].queryset = form.fields['author_id'].queryset.filter(catalog_id=catalog_id)

        if not form.is_valid():
            raise ValidationException(request, form)

        service = EntryService(
            Catalog.objects.get(pk=catalog_id), request.user
        )
        service.populate(entry, form)

        return SingleResponse(request, entry, serializer=EntrySerializer.Detailed)

    def delete(self, request, catalog_id: uuid.UUID, entry_id: uuid.UUID):
        entry = self._get_entry(request, catalog_id, entry_id)
        entry.hard_delete()

        return SingleResponse(request)
