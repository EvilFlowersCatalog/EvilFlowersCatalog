import re
from operator import itemgetter
from itertools import groupby
from django.core.exceptions import ValidationError
from django.db import models
from django.forms import CharField

"""
Based on the work of Mark Boszko
https://djangosnippets.org/snippets/3070/
"""


class MultiRangeField(models.CharField):
    description = "A multi-range of integers (e.g. page numbers 30, 41, 51-57, 68)"

    def __init__(self, *args, **kwargs):
        kwargs["max_length"] = 1000
        kwargs["help_text"] = "Comma-separated pages and page ranges."
        super(MultiRangeField, self).__init__(*args, **kwargs)

    def get_internal_type(self):
        return "CharField"

    def to_python(self, value: str) -> str:
        if not value:
            return ""
        # Validate
        if re.match("^[0-9, -]*$", value):
            return repack(depack(value))
        # else something's wrong.
        return value

    def get_prep_value(self, value):
        if not value:
            return ""
        return repack(depack(value))

    def value_to_string(self, obj):
        value = self.value_from_object(obj)
        return repack(depack(value))

    def formfield(self, **kwargs):
        defaults = {"form_class": MultiRangeFormField}
        defaults.update(kwargs)
        return super(MultiRangeField, self).formfield(**defaults)

    def clean(self, value, model_instance):
        if re.match("^[0-9, -]*$", value):
            return repack(depack(value))
        else:
            # Turn all other characters into commas, because it's probably a typo
            value = re.sub("[^0-9, -]", ",", value)
            return repack(depack(value))


class MultiRangeFormField(CharField):
    def validate(self, value):
        """
        Check if the value consts of valid page ranges,
        with only numbers, hyphens, commas, and spaces
        :param value:str
        :return:
        """
        if re.match("^[0-9, -]*$", value):
            return repack(depack(value))
        # Comment this out if you'd rather just have it auto-clean your entry
        else:
            raise ValidationError("Can only contain numbers, hyphens, commas, and spaces.")


def depack(value: str) -> list:
    """
    Unpacks a string representation of integers and ranges into a list of ints
    """
    page_list = []

    for part in value.strip().split(","):
        if "-" in part:
            # It's a range
            a, b = part.split("-")
            a, b = int(a), int(b)
            page_list.extend(range(a, b + 1))
        else:
            # Make sure that it contains a number before we add it.
            if re.match("[0-9]+", part):
                a = int(part)
                page_list.append(a)
    return page_list


def repack(page_list: list) -> str:
    """
    Returns a string representation from integers in a list
    """
    # Need to sort the list first, so that we can combine runs into ranges
    sorted_values = sorted(page_list, key=int)

    ranges = []

    def function(input_tuple):
        return input_tuple[0] - input_tuple[1]

    for key, group in groupby(enumerate(sorted_values), function):
        group = list(map(itemgetter(1), group))
        if len(group) > 1:
            ranges.append(range(group[0], group[-1]))  # under Python 3.x, switch to "range"
        else:
            ranges.append(group[0])

    ranges_strings = []
    for item in ranges:
        if isinstance(item, range):
            ranges_strings.append(f"{item[0]}-{item[-1] + 1}")  # 1-2
        else:
            ranges_strings.append(str(item))  # 1

    return ", ".join(ranges_strings)
