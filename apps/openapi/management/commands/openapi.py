import json

from django.core.management import BaseCommand

from apps.openapi.services import OpenApiService


class Command(BaseCommand):
    help = "Generate OpenAPI specification"

    def handle(self, *args, **options):
        service = OpenApiService()

        self.stdout.write(json.dumps(service.build()))
