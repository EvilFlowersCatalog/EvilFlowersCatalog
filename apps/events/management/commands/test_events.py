from django.core.management import BaseCommand
from django.db import models

from apps.events.services import get_event_broker


class Command(BaseCommand):
    help = "Test events"

    def handle(self, *args, **options):
        class SampleModel(models.Model):
            title = models.CharField(max_length=100)
            test = models.CharField(max_length=100)
            num_test = models.IntegerField()

            class Meta:
                managed = False

        model = SampleModel(title="TestTitle", test="test", num_test=123)

        event_broker = get_event_broker()
        event_broker.execute("test", model)
        # event_broker.execute(
        #     "test",
        #     {
        #         "args": [1, 2, 3],
        #         "kwargs": {"data": "thisisthedata"},
        #     },
        # )
        print("Test events done")
