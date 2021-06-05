import mimetypes
import uuid
from http import HTTPStatus

from django.db import transaction
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _

from apps.api.errors import ValidationException, ProblemDetailException
from apps.api.filters.entries import EntryFilter
from apps.api.forms.entries import CreateEntryForm, UpdateEntryForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.entries import EntrySerializer
from apps.api.views.base import SecuredView
from apps.core.models import Entry, Acquisition, Price, Catalog


class EntryPaginator(SecuredView):
    def get(self, request):
        entries = EntryFilter(request.GET, queryset=Entry.objects.all(), request=request).qs

        return PaginationResponse(
            request, entries, page=request.GET.get('page', 1), serializer=EntrySerializer.Base
        )


class EntryManagement(SecuredView):
    @method_decorator(transaction.atomic)
    def post(self, request, catalog_id: uuid.UUID):
        try:
            catalog = Catalog.objects.get(pk=catalog_id)
        except Catalog.DoesNotExist as e:
            raise ProblemDetailException(request, _("Catalog not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if catalog.creator != request.api_key.user:
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

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
