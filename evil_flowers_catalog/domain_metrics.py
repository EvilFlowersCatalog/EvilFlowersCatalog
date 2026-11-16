"""Catalog-specific metrics (IP-019).

Only what auto-instrumentation cannot see. Request rate, latency and errors
per route come from `http.server.request.duration`; these answer the questions
a route cannot:

- `evilflowers.readium.license.transitions` — how many loans were issued,
  returned, renewed, revoked, cancelled and expired (and whether the sweep or
  the borrow path expired them).
- `evilflowers.extraction.runs` / `evilflowers.extraction.run.duration` — how
  many extraction runs finished, by outcome, and how long they took.
- `evilflowers.extraction.jobs.superseded` — jobs of an earlier attempt of a
  reset run, by job and whether they stopped early or did the work for nothing.
- `evilflowers.extraction.ingest.refused` — batches a service posted that the
  forms refused (422), by endpoint and failing field (#181).
- `evilflowers.acquisitions.served` — publication files actually handed out, by
  format and path.
- `evilflowers.decider.answers` / `evilflowers.decider.sweep.duration` — answers
  the night decision sweeps stored (section relation, subject outcome) and how
  long each sweep ran (#190, #191).

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
extraction_jobs_superseded = _meter.create_counter(
    "evilflowers.extraction.jobs.superseded",
    unit="{job}",
    description="Extraction jobs of an earlier attempt of their run: stopped early by the service, or refused late.",
)
extraction_ingest_refused = _meter.create_counter(
    "evilflowers.extraction.ingest.refused",
    unit="{batch}",
    description="Extraction batches refused with 422, once per batch and failing form field (#181).",
)
acquisitions_served = _meter.create_counter(
    "evilflowers.acquisitions.served",
    unit="{file}",
    description="Publication files served to readers.",
)

decider_answers = _meter.create_counter(
    "evilflowers.decider.answers",
    unit="{answer}",
    description="Answers of the decider stored by the night sweeps (#190, #191), by task and answer.",
)
decider_sweep_duration = _meter.create_histogram(
    "evilflowers.decider.sweep.duration",
    unit="s",
    description="Duration of one night sweep of the decider (#190, #191).",
    explicit_bucket_boundaries_advisory=[1, 10, 60, 300, 600, 1800, 3600, 7200],
)

DECIDER_TASKS = frozenset({"edge", "subject", "mention", "shows", "harvest_match"})
DECIDER_ANSWERS = frozenset(
    {
        # edge (#190)
        "same",
        "pre",
        "example",
        "loose",
        "none",
        # subject (#191); accepted / rejected also harvest_match (#207)
        "filed",
        "pending",
        "skipped",
        "accepted",
        "rejected",
        # mention / shows (#206)
        "explains",
        "uses",
        "named",
        "yes",
        "no",
        # harvest_match (#207)
        "suggested",
    }
)

LICENSE_TRANSITIONS = frozenset({"issued", "returned", "renewed", "revoked", "cancelled", "expired"})
EXPIRY_PATHS = frozenset({"sweep", "on_read"})
EXTRACTION_OUTCOMES = frozenset({"done", "failed"})
EXTRACTION_JOBS = frozenset({"analyzer", "paragraphs", "figures", "tables", "equations"})
#: `early`: the service saw the new attempt and stopped; `late`: its results were refused after the work.
SUPERSEDED_STAGES = frozenset({"early", "late"})
INGEST_ENDPOINTS = frozenset({"paragraphs", "regions"})


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


def record_extraction_superseded(job: str, stage: str) -> None:
    """Not deferred to a commit: the refusal itself rolls nothing back that should cancel the count."""
    extraction_jobs_superseded.add(
        1,
        {
            # By kind: a per-batch job (`equations:0297-0304`, #150) must not become a label value.
            "evilflowers.extraction.job": _bounded(str(job).split(":", 1)[0], EXTRACTION_JOBS),
            "evilflowers.extraction.superseded.stage": _bounded(stage, SUPERSEDED_STAGES),
        },
    )


def record_extraction_ingest_refused(endpoint: str, fields) -> None:
    """One per failing field of a refused batch. Not deferred: a refusal commits nothing.

    `fields` are form field paths without list indexes (`paragraphs.bbox`,
    `paragraphs.sentences.quads`, `paragraphs.$body`): a closed set, the forms' own fields.
    """
    endpoint = _bounded(endpoint, INGEST_ENDPOINTS)
    for field in sorted(set(fields)):
        extraction_ingest_refused.add(
            1, {"evilflowers.extraction.endpoint": endpoint, "evilflowers.extraction.field": field[:64]}
        )


def record_acquisition_served(mime: str, via: str, delivery: str) -> None:
    """`via`: acquisition | user_acquisition; `delivery`: file | base64."""
    from apps.core.models import Acquisition

    attributes = {
        "evilflowers.acquisition.mime": _bounded(mime, frozenset(Acquisition.AcquisitionMIME.values)),
        "evilflowers.acquisition.via": _bounded(via, frozenset({"acquisition", "user_acquisition"})),
        "evilflowers.acquisition.delivery": _bounded(delivery, frozenset({"file", "base64"})),
    }
    telemetry.on_commit(lambda: acquisitions_served.add(1, attributes))


def record_decider_answers(task: str, answers) -> None:
    """Answers outside a night sweep (#207: harvest match suggestions and the librarian's verdicts)."""
    task = _bounded(task, DECIDER_TASKS)
    for answer, count in dict(answers).items():
        if count:
            decider_answers.add(
                count,
                {"evilflowers.decider.task": task, "evilflowers.decider.answer": _bounded(answer, DECIDER_ANSWERS)},
            )


def record_decider_sweep(task: str, seconds: float, answers) -> None:
    """One night sweep (#190, #191): its duration and the answers it stored, by answer. Not deferred: the sweep
    commits per batch."""
    task = _bounded(task, DECIDER_TASKS)
    decider_sweep_duration.record(max(seconds, 0.0), {"evilflowers.decider.task": task})
    for answer, count in dict(answers).items():
        if count:
            decider_answers.add(
                count,
                {"evilflowers.decider.task": task, "evilflowers.decider.answer": _bounded(answer, DECIDER_ANSWERS)},
            )


__all__ = [
    "record_acquisition_served",
    "record_decider_answers",
    "record_decider_sweep",
    "record_extraction_ingest_refused",
    "record_extraction_run",
    "record_extraction_superseded",
    "record_license_transition",
]
