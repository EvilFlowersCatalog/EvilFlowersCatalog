from django.core.validators import RegexValidator
from django.forms import CharField
from partial_date.fields import partial_date_re, PartialDate


class PartialDateFormField(CharField):
    default_validators = [RegexValidator(partial_date_re)]

    def to_python(self, value) -> PartialDate:
        return PartialDate(value)
