from django.shortcuts import render

from apps.opds.views.base import OpdsView


class RootView(OpdsView):
    def get(self, request, catalog_name: str):
        feeds = self.catalog.feeds.filter(parents__content__isnull=True)
        return render(request, 'opds/root.xml', {
            'catalog': self.catalog,
            'feeds': feeds
        }, content_type='application/atom+xml;profile=opds-catalog')
