"""Rebuild every entry's rating aggregate from its published reviews (IP-023).

python manage.py recompute_ratings          # rebuild
python manage.py recompute_ratings --check  # only count entries whose stored numbers are off
"""

from django.core.management import BaseCommand, CommandError

from apps.core.services.reviews import ReviewService


class Command(BaseCommand):
    help = "Recompute Entry.rating_count / rating_sum from published reviews"

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Report drift and exit non-zero, change nothing.")

    def handle(self, *args, **options):
        if options["check"]:
            drifted = ReviewService.drifted()
            if drifted:
                raise CommandError(f"{drifted} entries have a stale rating aggregate.")
            self.stdout.write(self.style.SUCCESS("Every rating aggregate matches its reviews."))
            return

        checked = ReviewService.recompute_all()
        self.stdout.write(self.style.SUCCESS(f"Recomputed the rating of {checked} entries."))
