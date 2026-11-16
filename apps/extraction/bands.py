"""Bands of extraction regions from a decider's answer (#189).

The workers (image-service#6, text-service#20) normally send the band with the region. The catalog computes one
only when a worker sent a probability without a band, and when `manage.py reband_regions` applies changed
thresholds to the stored probabilities. The thresholds come from calibration (knowledge#65), never guessed: they
are `EVILFLOWERS_DECIDER_BANDS`, empty by default, and a kind without thresholds is never banded here.

`EVILFLOWERS_DECIDER_BANDS` (JSON)::

    {"figure": {"hide": ["stamp", "decoration", "blank"], "upper": 0.9, "lower": 0.6},
     "table": {"upper": 0.7, "lower": 0.3}}

- figure: an answer listed in `hide` with p >= `upper` is hidden, with p >= `lower` it goes to review; any other
  answer, or a lower p, is kept.
- table: the table score (else p of "is this a data table?") >= `upper` is kept, < `lower` hidden, between
  goes to review.
"""

from typing import Optional

from django.conf import settings

from apps.extraction.models import Region


def thresholds(kind: str, config: Optional[dict] = None) -> Optional[dict]:
    config = settings.EVILFLOWERS_DECIDER_BANDS if config is None else config
    rule = (config or {}).get(kind)
    if not isinstance(rule, dict) or "upper" not in rule or "lower" not in rule:
        return None
    return rule


def band_for(
    kind: str,
    decision: Optional[str],
    p: Optional[float],
    score: Optional[float] = None,
    config: Optional[dict] = None,
) -> Optional[str]:
    """The band for a decider's answer, or None when there are no thresholds for the kind or nothing to band."""
    rule = thresholds(kind, config)
    if rule is None:
        return None
    upper, lower = float(rule["upper"]), float(rule["lower"])
    if kind == Region.Kind.FIGURE:
        if p is None or not decision:
            return None
        if decision not in (rule.get("hide") or []):
            return Region.Band.KEEP
        if p >= upper:
            return Region.Band.HIDE
        return Region.Band.REVIEW if p >= lower else Region.Band.KEEP
    if kind == Region.Kind.TABLE:
        value = score if score is not None else p
        if value is None:
            return None
        if value >= upper:
            return Region.Band.KEEP
        return Region.Band.HIDE if value < lower else Region.Band.REVIEW
    return None
