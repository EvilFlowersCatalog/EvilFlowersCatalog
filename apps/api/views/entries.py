import json
import mimetypes
import uuid
from functools import reduce
from http import HTTPStatus
from operator import or_

from django.db import transaction
from django.db.models import Q
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.services.entry_introspection_service import EntryIntrospectionService
from apps.core.errors import ValidationException, ProblemDetailException
from apps.api.filters.entries import EntryFilter
from apps.api.forms.entries import EntryForm, AcquisitionMetaForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.entries import EntrySerializer, AcquisitionSerializer
from apps.api.services.entry import EntryService
from apps.core.models import Entry, Acquisition, Price, Catalog
from apps.core.views import SecuredView


class EntryPaginator(SecuredView):
    def get(self, request):
        entries = EntryFilter(request.GET, queryset=Entry.objects.all(), request=request).qs.distinct()

        return PaginationResponse(request, entries, serializer=EntrySerializer.Base)


class EntryIntrospection(SecuredView):
    def get(self, request):

        service = EntryIntrospectionService(request.GET.get('driver'))
        result = service.resolve(request.GET.get('identifier'))

        return SingleResponse(request, result)


class EntryManagement(SecuredView):
    @method_decorator(transaction.atomic)
    def post(self, request, catalog_id: uuid.UUID):
        try:
            catalog = Catalog.objects.get(pk=catalog_id)
        except Catalog.DoesNotExist as e:
            raise ProblemDetailException(request, _("Catalog not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not has_object_permission('check_catalog_write', request.user, catalog):
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        form = EntryForm.create_from_request(request)
        form.fields['category_ids'].queryset = form.fields['category_ids'].queryset.filter(catalog=catalog)
        form.fields['author_id'].queryset = form.fields['author_id'].queryset.filter(catalog=catalog)

        if not form.is_valid():
            raise ValidationException(request, form)

        # Conflicts
        # TODO: this is suppose to be some kind of a setting
        conditions = [
            Q(title=form.cleaned_data['title'])
        ]
        if form.cleaned_data.get('identifiers', dict()).get('isbn'):
            conditions.append(
                Q(identifiers__isbn=form.cleaned_data['identifiers']['isbn'])
            )
        if form.cleaned_data.get('identifiers', dict()).get('doi'):
            conditions.append(
                Q(identifiers__doi=form.cleaned_data['identifiers']['doi'])
            )
        if Entry.objects.filter(catalog=catalog).filter(reduce(or_, conditions)).exists():
            raise ProblemDetailException(
                request,
                'Entry already exists!',
                HTTPStatus.CONFLICT,
                detail=_('Entry with same title, isbn or DOI already exists in catalog %s') % (catalog.title, )
            )

        entry = Entry(creator=request.user, catalog=catalog)
        service = EntryService(catalog, request.user)
        service.populate(entry, form)

        return SingleResponse(request, entry, serializer=EntrySerializer.Detailed, status=HTTPStatus.CREATED)


class EntryDetail(SecuredView):
    @staticmethod
    def get_entry(request, catalog_id: uuid.UUID, entry_id: uuid.UUID, checker: str = 'check_entry_manage') -> Entry:
        try:
            entry = Entry.objects.get(pk=entry_id, catalog_id=catalog_id)
        except Entry.DoesNotExist:
            raise ProblemDetailException(request, _("Entry not found"), status=HTTPStatus.NOT_FOUND)

        if not has_object_permission(checker, request.user, entry):
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return entry

    def get(self, request, catalog_id: uuid.UUID, entry_id: uuid.UUID):
        entry = self.get_entry(request, catalog_id, entry_id, 'check_entry_read')

        return SingleResponse(request, entry, serializer=EntrySerializer.Detailed)

    def post(self, request, catalog_id: uuid.UUID, entry_id: uuid.UUID):
        entry = self.get_entry(request, catalog_id, entry_id)

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
        entry = self.get_entry(request, catalog_id, entry_id)

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
        entry = self.get_entry(request, catalog_id, entry_id)
        entry.delete()

        return SingleResponse(request)
