import sys

from django.conf import settings
from django.utils import timezone
from django.views.generic.base import View

from apps.api.response import SingleResponse
from apps.core.models import Catalog, Entry, Acquisition, User


class StatusManagement(View):
    def get(self, request):
        response = {
            'timestamp': timezone.now(),
            'version': settings.VERSION,
            'instance': settings.INSTANCE_NAME,
            'stats': {
                'catalogs': Catalog.objects.count(),
                'entries': Entry.objects.count(),
                'acquisitions': Acquisition.objects.count(),
                'users': User.objects.count()
            },
        }

        if settings.DEBUG:
            response['python'] = sys.version

        return SingleResponse(request, response)
