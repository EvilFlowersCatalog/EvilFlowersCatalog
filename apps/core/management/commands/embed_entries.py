from django.core.management import BaseCommand, CommandError
from django.db.models import Q

from apps.core.models import Entry
from apps.core.search import embeddings
from apps.core.search.vectors import embed_entries


class Command(BaseCommand):
    help = (
        "Embed entries for semantic search through tei-embed (IP-021): title, authors, categories and "
        "summary. Runs in this process in batches; new and edited entries are embedded by Celery anyway."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--missing",
            action="store_true",
            help="Only entries without a vector of the current model (default: every entry).",
        )
        parser.add_argument("--catalog", help="Only entries of this catalog (url_name).")
        parser.add_argument("--batch", type=int, default=64, help="Entries per TEI round (default 64).")

    def handle(self, *args, **options):
        if not embeddings.enabled():
            raise CommandError("EVILFLOWERS_EMBEDDING_URL is not set")

        model = embeddings.model_id()
        entries = Entry.objects.all()
        if options["catalog"]:
            entries = entries.filter(catalog__url_name=options["catalog"])
        if options["missing"]:
            entries = entries.filter(Q(embedding__isnull=True) | ~Q(embedding_model=model))
        ids = list(entries.order_by("pk").values_list("pk", flat=True))

        self.stdout.write(f"Embedding {len(ids)} entries with {model}")
        done = 0
        for start in range(0, len(ids), options["batch"]):
            done += embed_entries(ids[start : start + options["batch"]])
            self.stdout.write(f"  {done}/{len(ids)}")
        self.stdout.write(self.style.SUCCESS(f"Embedded {done} entries"))
