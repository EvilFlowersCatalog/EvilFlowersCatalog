import pycountry
from django.conf import settings
from django.core.management import BaseCommand

from apps.core.models import Language, Currency


class Command(BaseCommand):
    help = "Create languages and currencies in the database"

    def handle(self, *args, **options):
        self._currencies()
        self._languages()

    def _currencies(self):
        for item in pycountry.currencies:
            try:
                currency = Currency.objects.get(code=item.alpha_3)
            except Currency.DoesNotExist:
                currency = Currency(code=item.alpha_3)

            currency.name = item.name
            currency.save()

    def _languages(self):
        for item in pycountry.languages:
            if not hasattr(item, "alpha_2"):
                continue

            try:
                language = Language.objects.get(alpha2=item.alpha_2)
            except Language.DoesNotExist:
                # New installations offer translations in the default language and English
                language = Language(
                    alpha2=item.alpha_2,
                    translatable=item.alpha_2 in (settings.EVILFLOWERS_DEFAULT_LANGUAGE, "en"),
                )

            language.name = item.name
            language.alpha3 = item.alpha_3
            language.save()
