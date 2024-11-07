from django.core.management import BaseCommand

from apps.core.models import Entry
from apps.core.models.entry import default_entry_config


class Command(BaseCommand):
    help = "Check if all entries have all config keys set"

    def handle(self, *args, **options):
        defaults = default_entry_config()

        for entry in Entry.objects.all():
            updated = False

            for key in defaults.keys():
                if key not in entry.config:
                    entry.config[key] = defaults[key]
                    self.stdout.write(f"Setting {key} to {defaults[key]} in {entry.id}")
                    updated = True

            if updated:
                entry.save()
