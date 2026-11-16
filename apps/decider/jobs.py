"""Job kinds of the decider's night sweeps (IP-031): applying one runs the sweep now."""

from datetime import timedelta

from django.conf import settings

from apps.tasks.contract import maintenance

_LIMIT = timedelta(seconds=settings.EVILFLOWERS_DECIDER_SWEEP_SECONDS + 10 * 60)

maintenance(
    "maintenance.decider_edge_sweep",
    "apps.decider.tasks.edge_sweep",
    "Type similar sections",
    "Ask the decision model how each similar section pair relates (same idea, prerequisite, example, loosely "
    "related, unrelated); untyped pairs first, EVILFLOWERS_DECIDER_EDGE_LIMIT a night (#190).",
    time_limit=_LIMIT,
)
maintenance(
    "maintenance.decider_mention_sweep",
    "apps.decider.tasks.mention_sweep",
    "Type concept mentions and figure references",
    "Ask the decision model whether a section explains, uses or only names each concept of its summary, and "
    "whether a passage next to a figure refers to it; untyped first, EVILFLOWERS_DECIDER_MENTION_LIMIT of each a "
    "night (#206).",
    time_limit=_LIMIT,
)
maintenance(
    "maintenance.decider_subject_sweep",
    "apps.decider.tasks.subject_sweep",
    "Suggest subjects",
    "Ask the decision model for a UDC subject of entries with at most one; file above the upper threshold, "
    "queue for review in between (#191).",
    time_limit=_LIMIT,
)
