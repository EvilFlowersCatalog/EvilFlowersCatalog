import os

from celery import Celery
from celery.schedules import crontab
from django.conf import settings

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "evil_flowers_catalog.settings.development")

app = Celery("evil_flowers_catalog")

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load task modules from all registered Django apps.
app.autodiscover_tasks()

app.conf.beat_schedule = {}

if settings.EVILFLOWERS_BACKUP_DESTINATION and settings.EVILFLOWERS_BACKUP_SCHEDULE:
    minute, hour, day_of_month, month_of_year, day_of_week = settings.EVILFLOWERS_BACKUP_SCHEDULE.strip().split(" ")
    app.conf.beat_schedule["backup"] = {
        "task": "tasks.tasks.backup",
        "schedule": crontab(
            minute=minute, hour=hour, day_of_month=day_of_month, month_of_year=month_of_year, day_of_week=day_of_week
        ),
    }
