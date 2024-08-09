from http import HTTPStatus
from uuid import UUID

from django.conf import settings
from django.http import HttpResponse
from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.core.models import Entry
from apps.opds.schema import AcquisitionEntry
from apps.opds.views.base import OpdsCatalogView


class EntryView(OpdsCatalogView):
    def get(self, request, catalog_name: str, entry_id: UUID):
        try:
            entry = Entry.objects.get(pk=entry_id, catalog__url_name=catalog_name)
        except Entry.DoesNotExist as e:
            raise ProblemDetailException(_("Entry not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        result = AcquisitionEntry.from_model(entry, True)

        return HttpResponse(
            result.to_xml(
                pretty_print=settings.DEBUG,
                encoding="UTF-8",
                standalone=True,
                skip_empty=True,
            ),
            content_type="application/atom+xml;type=entry;profile=opds-catalog",
        )
