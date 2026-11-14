"""Cross-references, figure vectors and the AGE graph for active extraction runs (IP-030 backfill).

New runs get these after they become active (`apps.extraction.tasks.post_process_run`); this fills in the runs
that were active before, or repeats a step. Every step is idempotent per run.

Examples:
    python manage.py link_extraction                    # every active run, all steps
    python manage.py link_extraction --steps references --steps regions
    python manage.py link_extraction --entry <uuid> --no-embed
"""

from django.core.management.base import BaseCommand

from apps.extraction import references, regions
from apps.extraction.models import ExtractionRun
from evil_flowers_catalog import tracing

STEPS = ("references", "regions", "graph")


class Command(tracing.TracedCommandMixin, BaseCommand):
    help = "Cross-references, figure documents and vectors, and the graph projection of active extraction runs."

    def add_arguments(self, parser):
        parser.add_argument("--entry", dest="entry_id", help="Only this publication.")
        parser.add_argument("--steps", action="append", choices=STEPS, help="Steps to run (default: all).")
        parser.add_argument("--no-embed", action="store_true", help="Set figure documents but do not embed them.")
        parser.add_argument("--limit", type=int, help="Stop after this many runs.")

    def handle(self, *args, **options):
        steps = options["steps"] or list(STEPS)
        runs = ExtractionRun.objects.filter(is_active=True).select_related("acquisition").order_by("finished_at")
        if options["entry_id"]:
            runs = runs.filter(acquisition__entry_id=options["entry_id"])
        if options["limit"]:
            runs = runs[: options["limit"]]

        totals: dict = {}
        for run in runs.iterator():
            line = [f"run {run.pk}"]
            if "references" in steps:
                counts = references.build(run)
                line.append(f"references {counts['resolved']}/{counts['found']}")
                totals["unresolved"] = totals.get("unresolved", 0) + counts["unresolved"]
            if "regions" in steps:
                counts = regions.index_run(run, embed=not options["no_embed"])
                line.append(f"figures {counts['documents']} documents, {counts['embedded']} embedded")
            if "graph" in steps:
                from apps.graph import projection

                if projection.available():
                    counts = projection.project_run(run)
                    line.append(f"graph {counts.get('sections', 0)} sections")
            self.stdout.write(" · ".join(line))
        if totals:
            self.stdout.write(f"unresolved references: {totals['unresolved']}")
