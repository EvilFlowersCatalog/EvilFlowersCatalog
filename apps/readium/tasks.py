from celery import Task

from apps.core.models import Entry, Acquisition
from evil_flowers_catalog.celery import app


@app.task(bind=True, queue="evilflowers_lcpencrypt_worker")
def drm_encrypted_content(self: Task, entry_id: str):
    try:
        entry = Entry.objects.get(pk=entry_id)
    except Entry.DoesNotExist:
        return

    for acquisition in entry.acquisitions.filter(relation=Acquisition.AcquisitionType.ACQUISITION):
        ...
