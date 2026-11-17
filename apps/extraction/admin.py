"""IP-035: the extraction pipeline (IP-018) in the console (Extraction module), read-only.

Paragraphs, sentences, links and locators run to millions of rows: those admins are `big` (planner
estimates instead of counts, no facets, no text search) and every relation is a raw-id field.
"""

from django.contrib import admin
from django.db.models import Count
from django.utils.html import format_html_join
from django.utils.translation import gettext_lazy as _

from apps.console.options import ConsoleModelAdmin, register
from apps.extraction.models import (
    Bibitem,
    ExtractionRun,
    Locator,
    Paragraph,
    ParagraphLink,
    Reference,
    Region,
    SectionSimilarity,
    Sentence,
)


@register(ExtractionRun, module="extraction")
class ExtractionRunAdmin(ConsoleModelAdmin):
    list_display = ("id", "acquisition", "pipeline_version", "status", "is_active", "page_count", "finished_at")
    list_filter = ("status", "is_active", "pipeline_version")
    readonly_fields = ("paragraph_decisions",)
    list_select_related = ("acquisition__entry",)
    attention = ((_("extraction runs failed"), {"status": "failed", "is_active": True}),)

    @admin.display(description=_("Paragraph decisions"))
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


@register(Region, module="extraction")
class RegionAdmin(ConsoleModelAdmin):
    list_display = ("id", "run", "kind", "page", "seq", "category", "band", "decision")
    list_filter = ("kind", "band")
    search_fields = ("locator_key",)


@register(Paragraph, module="extraction")
class ParagraphAdmin(ConsoleModelAdmin):
    big = True
    list_display = ("locator_key", "run", "page", "seq", "decision", "band")
    list_filter = ("band",)
    # `__exact`, not `=` (iexact → UPPER(), which skips the index on 400 000 rows).
    search_fields = ("locator_key__exact",)


@register(Sentence, module="extraction")
class SentenceAdmin(ConsoleModelAdmin):
    big = True
    list_display = ("id", "paragraph", "seq", "start", "end", "page")


@register(Locator, module="extraction")
class LocatorAdmin(ConsoleModelAdmin):
    big = True
    list_display = ("key", "entry", "kind", "source_page", "detached_at")
    list_filter = ("kind",)
    search_fields = ("key__exact",)


@register(ParagraphLink, module="extraction")
class ParagraphLinkAdmin(ConsoleModelAdmin):
    big = True
    list_display = ("id", "source", "target", "kind", "score")
    list_filter = ("kind",)


@register(SectionSimilarity, module="extraction")
class SectionSimilarityAdmin(ConsoleModelAdmin):
    list_display = ("source", "source_section", "target", "target_section", "score")


@register(Reference, module="extraction")
class ReferenceAdmin(ConsoleModelAdmin):
    big = True
    list_display = ("id", "run", "kind", "anchor", "number")
    list_filter = ("kind",)


@register(Bibitem, module="extraction")
class BibitemAdmin(ConsoleModelAdmin):
    big = True
    list_display = ("run", "seq", "number", "doi", "isbn", "entry", "page")
    search_fields = ("doi__exact", "isbn__exact")
