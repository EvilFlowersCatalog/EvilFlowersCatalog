import sys
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from django.views.generic.base import View

from apps.api.response import SingleResponse
from apps.core.models import Catalog, Entry, Acquisition, User


class StatusManagement(View):
    def get(self, request):
        build_file = Path(f"{settings.BASE_DIR}/BUILD.txt")
        version_file = Path(f"{settings.BASE_DIR}/VERSION.txt")

        response = {
            'timestamp': timezone.now(),
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

        if build_file.exists():
            with open(build_file) as f:
                response['build'] = f.readline().replace('\n', '')

        if version_file.exists():
            with open(version_file) as f:
                response['version'] = f.readline().replace('\n', '')

        return SingleResponse(request, response)
