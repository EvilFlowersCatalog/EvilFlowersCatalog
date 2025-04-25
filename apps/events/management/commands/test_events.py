from django.core.management import BaseCommand

from apps.events.services import get_event_broker


class Command(BaseCommand):
    help = "Test events"

    def handle(self, *args, **options):
        event_broker = get_event_broker()
        event_broker.execute(
            "test",
            {
                "args": [1, 2, 3],
                "kwargs": {"data": "thisisthedata"},
            },
        )
        print("Test events done")
