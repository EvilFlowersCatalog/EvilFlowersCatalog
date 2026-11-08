from django.core.management import BaseCommand
from django.db import transaction

from apps.core.search.ddl import ensure_search_objects
from apps.core.search.index import language_configs, refresh_search_vectors


class Command(BaseCommand):
    help = (
        "Create missing text search configurations (e.g. Slovak/Czech once the hunspell dictionaries are "
        "installed in the PostgreSQL image) and rebuild the full-text search vector of every entry."
    )

    def handle(self, *args, **options):
        with transaction.atomic():
            configs = ensure_search_objects()
            count = refresh_search_vectors()

        self.stdout.write(f"Text search configurations: {', '.join(configs)}")
        mapped = ", ".join(f"{code}→{config}" for code, config in language_configs().items())
        self.stdout.write(f"Languages with a stemmer: {mapped or 'none'} (others use evilflowers_simple)")
        self.stdout.write(self.style.SUCCESS(f"Reindexed {count} entries"))
        self.stdout.write(
            "Restart the application and the Celery workers: running processes cache the list of "
            "configurations and keep building queries without new ones until restarted."
        )
