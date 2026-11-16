"""Re-band paragraphs from their stored decider answers after the `paragraph` rule changes (#208).

No re-extraction: the band is recomputed from `decision` and `decision_p` with the `paragraph` rule of
`EVILFLOWERS_DECIDER_BANDS` (or `--thresholds`). Passage search and the assistant follow at once; section
similarity with the weekly `extraction-similarity-rebuild` (or `manage.py link_extraction --steps similarity`), the
graph with the nightly `graph-rebuild`.

Examples:
    python manage.py reband_paragraphs --dry-run
    python manage.py reband_paragraphs --thresholds '{"paragraph": {"hide": ["references", "toc"], "upper": 0.9}}'
"""

import json
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.extraction import bands
from apps.extraction.models import Paragraph
from evil_flowers_catalog import tracing


class Command(tracing.TracedCommandMixin, BaseCommand):
    help = "Recompute the decider band of paragraphs of active runs from their stored probabilities."

    def add_arguments(self, parser):
        parser.add_argument("--entry", dest="entry_id", help="Only this publication.")
        parser.add_argument("--thresholds", help="JSON in the shape of EVILFLOWERS_DECIDER_BANDS, instead of it.")
        parser.add_argument("--dry-run", action="store_true", help="Count the changes, write nothing.")

    def handle(self, *args, **options):
        try:
            config = json.loads(options["thresholds"]) if options["thresholds"] else settings.EVILFLOWERS_DECIDER_BANDS
        except json.JSONDecodeError as error:
            raise CommandError(f"--thresholds is not JSON: {error}") from error
        rule = (config or {}).get("paragraph")
        if not isinstance(rule, dict) or "upper" not in rule:
            raise CommandError("No `paragraph` rule: set EVILFLOWERS_DECIDER_BANDS or pass --thresholds.")

        queryset = Paragraph.objects.filter(
            run__is_active=True, decision__isnull=False, decision_p__isnull=False
        ).only("id", "band", "decision", "decision_p")
        if options["entry_id"]:
            queryset = queryset.filter(run__acquisition__entry_id=options["entry_id"])

        changed, transitions = [], Counter()
        for paragraph in queryset.iterator(chunk_size=5000):
            band = bands.paragraph_band(paragraph.decision, paragraph.decision_p, config)
            if band is None or band == paragraph.band:
                continue
            transitions[f"{paragraph.decision} {paragraph.band or 'none'} -> {band}"] += 1
            paragraph.band = band
            changed.append(paragraph)

        for transition, count in sorted(transitions.items()):
            self.stdout.write(f"{transition}: {count}")
        self.stdout.write(f"{len(changed)} paragraphs change band")
        if not options["dry_run"] and changed:
            Paragraph.objects.bulk_update(changed, ["band"], batch_size=2000)
