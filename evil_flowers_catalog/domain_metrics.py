"""Catalog-specific metrics (IP-019).

Only what auto-instrumentation cannot see. Request rate, latency and errors
per route come from `http.server.request.duration`; these answer the questions
a route cannot:

- `evilflowers.readium.license.transitions` — how many loans were issued,
  returned, renewed, revoked, cancelled and expired (and whether the sweep or
  the borrow path expired them).
- `evilflowers.extraction.runs` / `evilflowers.extraction.run.duration` — how
  many extraction runs finished, by outcome, and how long they took.
- `evilflowers.acquisitions.served` — publication files actually handed out, by
  format and path.

Attribute values are closed sets. No user, entry, license or acquisition ids
and no URLs: each would create a time series per object in Mimir.

Every recording waits for the surrounding transaction to commit, so a rolled
back borrow is never counted.
"""

from evil_flowers_catalog import telemetry

_meter = telemetry.get_meter("evilflowers.catalog")

license_transitions = _meter.create_counter(
    "evilflowers.readium.license.transitions",
    unit="{license}",
    description="Readium LCP license state transitions.",
)
extraction_runs = _meter.create_counter(
    "evilflowers.extraction.runs",
    unit="{run}",
    description="Finished document extraction runs.",
)
extraction_run_duration = _meter.create_histogram(
    "evilflowers.extraction.run.duration",
    unit="s",
    description="Time from the start of an extraction run to its last job report.",
    explicit_bucket_boundaries_advisory=[1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 14400],
)
acquisitions_served = _meter.create_counter(
    "evilflowers.acquisitions.served",
    unit="{file}",
    description="Publication files served to readers.",
)

LICENSE_TRANSITIONS = frozenset({"issued", "returned", "renewed", "revoked", "cancelled", "expired"})
EXPIRY_PATHS = frozenset({"sweep", "on_read"})
EXTRACTION_OUTCOMES = frozenset({"done", "failed"})


def _bounded(value, allowed) -> str:
    return value if value in allowed else "_OTHER"


def record_license_transition(transition: str, expiry_path: str = None) -> None:
    attributes = {"evilflowers.readium.transition": _bounded(transition, LICENSE_TRANSITIONS)}
    if expiry_path:
        attributes["evilflowers.readium.expiry_path"] = _bounded(expiry_path, EXPIRY_PATHS)
    telemetry.on_commit(lambda: license_transitions.add(1, attributes))


def record_extraction_run(outcome: str, started_at=None, finished_at=None) -> None:
    attributes = {"evilflowers.extraction.outcome": _bounded(outcome, EXTRACTION_OUTCOMES)}

    def _record():
        extraction_runs.add(1, attributes)
        if started_at and finished_at:
            extraction_run_duration.record(max((finished_at - started_at).total_seconds(), 0.0), attributes)

    telemetry.on_commit(_record)


def record_acquisition_served(mime: str, via: str, delivery: str) -> None:
    """`via`: acquisition | user_acquisition; `delivery`: file | base64."""
    from apps.core.models import Acquisition

    attributes = {
        "evilflowers.acquisition.mime": _bounded(mime, frozenset(Acquisition.AcquisitionMIME.values)),
        "evilflowers.acquisition.via": _bounded(via, frozenset({"acquisition", "user_acquisition"})),
        "evilflowers.acquisition.delivery": _bounded(delivery, frozenset({"file", "base64"})),
    }
    telemetry.on_commit(lambda: acquisitions_served.add(1, attributes))


__all__ = [
    "record_acquisition_served",
    "record_extraction_run",
    "record_license_transition",
]
