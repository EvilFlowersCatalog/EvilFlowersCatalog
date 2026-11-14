import django_filters
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from apps.tasks.models import JobProtocol
from apps.tasks.services import latest_ids, stale_q


class CsvFilter(django_filters.CharFilter):
    """`a,b,c` → `field__in`."""

    def filter(self, qs, value):
        if not value:
            return qs
        values = [token.strip() for token in value.split(",") if token.strip()]
        return qs.filter(**{f"{self.field_name}__in": values}) if values else qs


class JobFilter(django_filters.FilterSet):
    """Background jobs (IP-031). Administrators only; the view checks before filtering."""

    kind = CsvFilter(field_name="kind", help_text="Job kinds, comma-separated (`GET /api/v1/jobs/kinds`).")
    status = CsvFilter(
        field_name="status",
        help_text="Statuses, comma-separated: queued, received, started, retrying, succeeded, failed, revoked, "
        "rejected, lost.",
    )
    stale = django_filters.BooleanFilter(method="filter_stale", help_text="Only (or no) stale queued jobs.")
    latest = django_filters.BooleanFilter(
        method="filter_latest", help_text="Only the newest job per kind and subject (the subject's current state)."
    )
    without_kind = django_filters.BooleanFilter(
        method="filter_without_kind", help_text="Include (true) or exclude (false) tasks that have no job kind."
    )
    catalog_id = django_filters.UUIDFilter(field_name="catalog_id")
    entry_id = django_filters.UUIDFilter(field_name="entry_id")
    subject_type = django_filters.CharFilter(method="filter_subject_type", help_text="`app_label.model`.")
    subject_id = django_filters.UUIDFilter(field_name="subject_id")
    parent_id = django_filters.UUIDFilter(field_name="parent_id")
    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains", help_text="Celery task name.")
    query = django_filters.CharFilter(method="filter_query", help_text="Entry title (accent-insensitive).")
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = JobProtocol
        fields = []

    def filter_stale(self, qs, name, value):
        if value is None:
            return qs
        return qs.filter(stale_q()) if value else qs.exclude(stale_q())

    def filter_latest(self, qs, name, value):
        return qs.filter(id__in=latest_ids()) if value else qs

    def filter_without_kind(self, qs, name, value):
        if value is None:
            return qs
        return qs if value else qs.filter(kind__isnull=False)

    def filter_subject_type(self, qs, name, value):
        app_label, _, model = (value or "").partition(".")
        content_type = ContentType.objects.filter(app_label=app_label, model=model).first()
        return qs.filter(subject_type=content_type) if content_type else qs.none()

    def filter_query(self, qs, name, value):
        from apps.core.models import Entry

        if not value:
            return qs
        entries = Entry.objects.filter(Q(title__unaccent__icontains=value)).values("id")
        return qs.filter(entry_id__in=entries)
