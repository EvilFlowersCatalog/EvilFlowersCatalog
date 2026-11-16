"""Re-band figures and tables from their stored decider answers after a threshold change (#189).

No re-extraction: the band is recomputed from `decision`, `decision_p` and `decision_score` with the thresholds of
`EVILFLOWERS_DECIDER_BANDS` (or `--thresholds`, to try values before setting them). Regions without a probability
or score keep the band the worker sent (e.g. `decision: repeated`, image-service's repeated-image rule). Runs where
a region became hidden or shown again get their search documents and vectors redone (`index_run`); the graph
follows with the nightly `graph-rebuild` or `link_extraction --steps graph`.

Examples:
    python manage.py reband_regions --dry-run
    python manage.py reband_regions --kind figure --thresholds '{"figure": {"hide": ["stamp"], "upper": 0.9, "lower": 0.6}}'
    python manage.py reband_regions --entry <uuid> --no-embed
"""

import json
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.extraction import bands, regions
from apps.extraction.models import ExtractionRun, Region
from evil_flowers_catalog import tracing

KINDS = (Region.Kind.FIGURE, Region.Kind.TABLE)
#: Decisions a worker made without a model probability; never re-banded here.
RULE_DECISIONS = ("repeated",)


class Command(tracing.TracedCommandMixin, BaseCommand):
    help = "Recompute the decider band of figures and tables of active runs from their stored probabilities."

    def add_arguments(self, parser):
        parser.add_argument("--kind", action="append", choices=KINDS, help="Region kinds (default: both).")
        parser.add_argument("--entry", dest="entry_id", help="Only this publication.")
        parser.add_argument("--thresholds", help="JSON in the shape of EVILFLOWERS_DECIDER_BANDS, instead of it.")
        parser.add_argument("--dry-run", action="store_true", help="Count the changes, write nothing.")
        parser.add_argument("--no-embed", action="store_true", help="Redo search documents but do not embed them.")

    def handle(self, *args, **options):
        try:
            config = json.loads(options["thresholds"]) if options["thresholds"] else settings.EVILFLOWERS_DECIDER_BANDS
        except json.JSONDecodeError as error:
            raise CommandError(f"--thresholds is not JSON: {error}") from error
        kinds = [kind for kind in (options["kind"] or KINDS) if bands.thresholds(kind, config) is not None]
        if not kinds:
            raise CommandError(
                "No thresholds for the requested kinds: set EVILFLOWERS_DECIDER_BANDS or pass --thresholds."
            )

        queryset = (
            Region.objects.filter(run__is_active=True, kind__in=kinds)
            .filter(Q(decision_p__isnull=False) | Q(decision_score__isnull=False))
            .exclude(decision__in=RULE_DECISIONS)
            .only("id", "run_id", "kind", "band", "decision", "decision_p", "decision_score")
        )
        if options["entry_id"]:
            queryset = queryset.filter(run__acquisition__entry_id=options["entry_id"])

        changed, flipped, transitions = [], set(), Counter()
        for region in queryset.iterator(chunk_size=2000):
            band = bands.band_for(region.kind, region.decision, region.decision_p, region.decision_score, config)
            if band is None or band == region.band:
                continue
            transitions[f"{region.kind} {region.band or 'none'} -> {band}"] += 1
            if (region.band == Region.Band.HIDE) != (band == Region.Band.HIDE):
                flipped.add(region.run_id)
            region.band = band
            changed.append(region)

        for transition, count in sorted(transitions.items()):
            self.stdout.write(f"{transition}: {count}")
        self.stdout.write(f"{len(changed)} regions change band, {len(flipped)} runs to re-index")
        if options["dry_run"] or not changed:
            return

        Region.objects.bulk_update(changed, ["band"], batch_size=1000)
        for run in ExtractionRun.objects.filter(pk__in=flipped).select_related("acquisition"):
            counts = regions.index_run(run, embed=not options["no_embed"])
            self.stdout.write(f"run {run.pk}: {counts}")
