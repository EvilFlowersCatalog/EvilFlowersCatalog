from uuid import UUID

from celery import signature
from django.conf import settings
from django.core.management import BaseCommand

from apps.core.models import Acquisition


class Command(BaseCommand):
    help = "Add Readium lcpencrypt signing job to the queue"

    def add_arguments(self, parser):
        parser.add_argument("--id", type=UUID, default=None, help="Acquisition UUID")

    def handle(self, *args, **options):
        try:
            acquisition = Acquisition.objects.get(pk=options["id"])
        except Acquisition.DoesNotExist:
            self.stderr.write("Acquisition does not exists")
            return

        signature(
            "evilflowers_lcpencrypt_worker.lcpencrypt",
            kwargs={
                "input_file": acquisition.content.name,
                "contentid": str(acquisition.id),
                "storage": settings.EVILFLOWERS_READIUM_DATADIR,
                "filename": f"{acquisition.pk}.lcp.pdf",  # TODO: meh
                "lcpsv": settings.EVILFLOWERS_READIUM_LCPSV_URL,
                "notify": settings.EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL,
                "url": f"{settings.EVILFLOWERS_READIUM_BASE_URL}/{acquisition.pk}.lcp.pdf",  # TODO: Questionable S3 support
                "verbose": True,
            },
            immutable=True,
            # Queue have to be defined explicitly, settings.CELERY_TASK_ROUTES is ignored for some reason
            queue="evilflowers_lcpencrypt_worker",
        ).apply_async()
