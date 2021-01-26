from http import HTTPStatus

import requests
from django.core.management import BaseCommand

from apps.core.models import Language, Currency


class Command(BaseCommand):
    help = 'Download list of languages from restcountries.eu and currencies from openexchangerates.org'

    def handle(self, *args, **options):
        self._currencies()
        self._languages()

    def _currencies(self):
        response = requests.get('https://openexchangerates.org/api/currencies.json')

        if response.status_code != HTTPStatus.OK:
            self.stderr.write("Invalid response from restcountries.eu")
            return

        for iso_code, currency in response.json().items():
            Currency.objects.get_or_create(
                code=iso_code,
                name=currency
            )

    def _languages(self):
        response = requests.get('https://restcountries.eu/rest/v2/all')

        if response.status_code != HTTPStatus.OK:
            self.stderr.write("Invalid response from restcountries.eu")
            return

        countries = response.json()
        for country in countries:
            for language in country['languages']:
                if language.get('iso639_1'):
                    Language.objects.get_or_create(
                        code=language['iso639_1'],
                        name=language['name'],
                        native_name=language.get('nativeName'),
                    )
