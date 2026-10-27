from django_api_forms import Form, BooleanField


class LanguageForm(Form):
    translatable = BooleanField()
