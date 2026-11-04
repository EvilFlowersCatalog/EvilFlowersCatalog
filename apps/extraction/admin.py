from django.contrib import admin

from apps.extraction.models import ExtractionRun, Region


@admin.register(ExtractionRun)
class ExtractionRunAdmin(admin.ModelAdmin):
    list_display = ("id", "acquisition", "pipeline_version", "status", "is_active", "page_count", "finished_at")
    list_filter = ("status", "is_active", "pipeline_version")
    raw_id_fields = ("acquisition",)


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ("id", "run", "kind", "page", "seq", "category")
    list_filter = ("kind",)
    raw_id_fields = ("run",)
