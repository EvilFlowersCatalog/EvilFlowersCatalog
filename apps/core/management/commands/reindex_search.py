from django.core.management import BaseCommand
from django.db import transaction

from apps.core.search.ddl import ensure_bm25, ensure_search_objects
from apps.core.search.index import language_configs, refresh_search_vectors


class Command(BaseCommand):
    help = (
        "Create missing text search configurations (e.g. Slovak/Czech once the hunspell dictionaries are "
        "installed in the PostgreSQL image), rebuild the full-text index and the denormalised search text of "
        "every entry, and create the BM25 index when pg_search is available (IP-020, IP-021). Entry vectors "
        "are rebuilt by `embed_entries`."
    )

    def handle(self, *args, **options):
        with transaction.atomic():
            configs = ensure_search_objects()
            count = refresh_search_vectors()
        bm25 = ensure_bm25()

        self.stdout.write(f"Text search configurations: {', '.join(configs)}")
        mapped = ", ".join(f"{code}→{config}" for code, config in language_configs().items())
        self.stdout.write(f"Languages with a stemmer: {mapped or 'none'} (others use evilflowers_simple)")
        self.stdout.write(f"BM25 (pg_search): {'yes' if bm25 else 'no, ranking with ts_rank_cd'}")
        self.stdout.write(self.style.SUCCESS(f"Reindexed {count} entries"))
        self.stdout.write(
            "Restart the application and the Celery workers: running processes cache the list of "
            "configurations and whether BM25 exists until restarted."
        )
