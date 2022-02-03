import os

from django.utils import timezone
from django.views.generic.base import View

from apps.api.response import SingleResponse
from apps.core.models import Catalog, Entry, Acquisition, User


class StatusManagement(View):
    def get(self, request):
        return SingleResponse(request, {
            'timestamp': timezone.now(),
            'version': os.getenv('VERSION'),
            'stats': {
                'catalogs': Catalog.objects.count(),
                'entries': Entry.objects.count(),
                'acquisitions': Acquisition.objects.count(),
                'users': User.objects.count()
            }
        })
