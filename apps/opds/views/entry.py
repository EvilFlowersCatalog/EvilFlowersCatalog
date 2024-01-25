from http import HTTPStatus
from uuid import UUID

from django.shortcuts import render
from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.core.models import Entry
from apps.opds.views.base import OpdsView


class EntryView(OpdsView):
    def get(self, request, catalog_name: str, entry_id: UUID):
        try:
            entry = Entry.objects.get(pk=entry_id, catalog__url_name=catalog_name)
        except Entry.DoesNotExist as e:
            raise ProblemDetailException(
                _("Entry not found"), status=HTTPStatus.NOT_FOUND, previous=e
            )

        return render(
            request,
            "opds/_partials/entry.xml",
            {"entry": entry, "is_complete": True},
            content_type="application/atom+xml;type=entry;profile=opds-catalog",
        )
