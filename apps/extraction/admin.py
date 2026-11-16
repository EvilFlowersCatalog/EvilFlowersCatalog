from django.contrib import admin
from django.db.models import Count
from django.utils.html import format_html_join

from apps.extraction.models import ExtractionRun, Region


@admin.register(ExtractionRun)
class ExtractionRunAdmin(admin.ModelAdmin):
    list_display = ("id", "acquisition", "pipeline_version", "status", "is_active", "page_count", "finished_at")
    list_filter = ("status", "is_active", "pipeline_version")
    raw_id_fields = ("acquisition",)
    readonly_fields = ("paragraph_decisions",)

    @admin.display(description="Paragraph decisions")
    def paragraph_decisions(self, run: ExtractionRun):
        """#208: paragraphs per decided kind and band (undecided as `-`)."""
        rows = (
            run.paragraphs.values("decision", "band")
            .annotate(count=Count("id"))
            .order_by("-count", "decision", "band")
        )
        return format_html_join(
            "\n",
            "<div>{} / {}: {}</div>",
            ((row["decision"] or "-", row["band"] or "-", row["count"]) for row in rows),
        )


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ("id", "run", "kind", "page", "seq", "category")
    list_filter = ("kind",)
    raw_id_fields = ("run",)
