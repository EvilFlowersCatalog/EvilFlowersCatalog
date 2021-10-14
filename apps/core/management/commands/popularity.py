import logging

from django.conf import settings
from django.core.management import BaseCommand
from redis import Redis

from apps.core.models import Entry


class Command(BaseCommand):
    help = 'Update popularity according the redis cache'

    def handle(self, *args, **options):
        logger = logging.getLogger(__name__)
        logger.info("Popularity sync executed")
        redis = Redis(
            host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DATABASE
        )
        for entry in Entry.objects.all():
            entry.popularity = redis.pfcount(f'popularity:{entry.pk}')
            entry.save()
            logger.debug(f"HyperLogLog popularity sync:{entry.pk}={entry.popularity}")
