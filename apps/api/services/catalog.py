from apps.api.forms.catalogs import CatalogForm
from apps.core.models import Catalog, UserCatalog


class CatalogService:
    def populate(self, catalog: Catalog, form: CatalogForm) -> Catalog:
        form.populate(catalog)
        catalog.save()

        if 'users' in form.cleaned_data:
            UserCatalog.objects.filter(catalog=catalog).delete()
            for item in form.cleaned_data['users']:
                user_catalog = UserCatalog.objects.create(
                    catalog=catalog,
                    user=item['user_id'],
                    mode=item['mode']
                )
                user_catalog.save()

        return catalog
