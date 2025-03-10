from celery import shared_task
from django.conf import settings
from django.core.management import call_command


@shared_task
def backup():
    if settings.EVILFLOWERS_BACKUP_DESTINATION:
        call_command(
            "backup",
            destination=settings.EVILFLOWERS_BACKUP_DESTINATION,
        )
