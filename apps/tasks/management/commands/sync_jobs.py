"""Create or refresh jobs from the providers' domain state (IP-031 rev. 2).

Run after deploying: every LCP package, extraction run … gets its job, also ones nobody declared
(they carry no intent and never dispatch). Beat runs the same daily (`apps.tasks.tasks.sync_jobs`).
"""

from django.core.management.base import BaseCommand

from apps.tasks.application.facade import facade


class Command(BaseCommand):
    help = "Create or refresh jobs from the providers' domain state (JobDefinition.discover)"

    def add_arguments(self, parser):
        parser.add_argument("--kind", help="Only this job kind")

    def handle(self, *args, **options):
        observed = facade.discover(options.get("kind"))
        self.stdout.write(f"observed {observed} subject(s)")
