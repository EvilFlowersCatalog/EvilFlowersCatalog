"""Generate chapter summaries for the assistant (IP-024 §7, #119).

    python manage.py summarise_chapters --entry <uuid>          # one publication, in this process
    python manage.py summarise_chapters --limit 20              # the next 20 publications without summaries
    python manage.py summarise_chapters --limit 20 --queue      # the same, as Celery tasks

On CPU inference a chapter takes about a minute, so work through the library in batches. Publications lent
through Readium LCP are skipped (their text never reaches the model).
"""

from django.core.management.base import BaseCommand

from apps.assistant.chapters import summarise_entry


class Command(BaseCommand):
    help = "Summarise the chapters of publications for the assistant."

    def add_arguments(self, parser):
        parser.add_argument("--entry", action="append", default=[], help="Entry id; repeat for several.")
        parser.add_argument("--limit", type=int, default=0, help="Publications without summaries to do now.")
        parser.add_argument("--force", action="store_true", help="Summarise again even when nothing changed.")
        parser.add_argument("--queue", action="store_true", help="Queue Celery tasks instead of running here.")

    def handle(self, *args, **options):
        from apps.assistant import tasks
        from apps.core.models import Entry

        entry_ids = list(options["entry"])
        if options["limit"]:
            candidates = (
                Entry.objects.filter(table_of_contents__isnull=False, chapter_summaries__isnull=True)
                .exclude(table_of_contents=[])
                .select_related("catalog")
                .order_by("-popularity", "created_at")
            )
            picked = []
            for entry in candidates.iterator(chunk_size=100):
                if len(picked) >= options["limit"]:
                    break
                # LCP publications never get summaries; picking them would fill every batch.
                if not entry.read_config("readium_enabled"):
                    picked.append(str(entry.pk))
            entry_ids += picked

        if not entry_ids:
            self.stderr.write("Nothing to do: pass --entry or --limit.")
            return

        for entry_id in entry_ids:
            if options["queue"]:
                tasks.summarise_chapters.delay(str(entry_id), options["force"])
                self.stdout.write(f"queued {entry_id}")
            else:
                self.stdout.write(f"{entry_id}: {summarise_entry(entry_id, force=options['force'])}")
