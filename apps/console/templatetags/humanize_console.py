from django import template

register = template.Library()


@register.filter
def tone(value):
    """The initials tile colour (1–8) of a name; see `apps.console.records.tone`."""
    from apps.console.records import tone as tone_of

    return tone_of(value)


@register.filter
def compact_number(value):
    """1234 → 1.2k, 2521494 → 2.5M; small numbers unchanged."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    for limit, suffix in ((1_000_000_000, "G"), (1_000_000, "M"), (1_000, "k")):
        if abs(number) >= limit:
            text = f"{number / limit:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{int(number)}"
