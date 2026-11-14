from datetime import timedelta

import django_filters
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils import timezone

from apps.tasks.models import Job, JobRun


class CsvFilter(django_filters.CharFilter):
    """`a,b,c` → `field__in`."""

    def filter(self, qs, value):
        values = [token.strip() for token in (value or "").split(",") if token.strip()]
        return qs.filter(**{f"{self.field_name}__in": values}) if values else qs


def _subject_type(qs, value):
    app_label, _, model = (value or "").partition(".")
    content_type = ContentType.objects.filter(app_label=app_label, model=model).first()
    return qs.filter(subject_type=content_type) if content_type else qs.none()


def _entry_title(qs, value):
    from apps.core.models import Entry

    if not value:
        return qs
    return qs.filter(entry_id__in=Entry.objects.filter(title__unaccent__icontains=value).values("id"))


class JobFilter(django_filters.FilterSet):
    """Jobs (IP-031 rev. 2). Administrators only; the view checks before filtering."""

    kind = CsvFilter(field_name="kind", help_text="Job kinds, comma-separated (`GET /api/v1/jobs/kinds`).")
    state = CsvFilter(
        field_name="state",
        help_text="pending, blocked, queued, running, cancelling, succeeded, failed, cancelled (comma-separated).",
    )
    desired_state = django_filters.CharFilter(field_name="desired_state", help_text="completed or cancelled.")
    stale = django_filters.BooleanFilter(
        method="filter_stale", help_text="Queued past EVILFLOWERS_JOBS_STALE_MINUTES."
    )
    catalog_id = django_filters.UUIDFilter(field_name="catalog_id")
    entry_id = django_filters.UUIDFilter(field_name="entry_id")
    subject_type = django_filters.CharFilter(method="filter_subject_type", help_text="`app_label.model`.")
    subject_id = django_filters.UUIDFilter(field_name="subject_id")
    cause_id = django_filters.UUIDFilter(field_name="cause_id", help_text="Jobs declared by this job's outcome.")
    query = django_filters.CharFilter(method="filter_query", help_text="Entry title (accent-insensitive).")
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = Job
        fields = []

    @staticmethod
    def stale_q() -> Q:
        cutoff = timezone.now() - timedelta(minutes=settings.EVILFLOWERS_JOBS_STALE_MINUTES)
        return Q(state="queued", last_transition_at__lt=cutoff)

    def filter_stale(self, qs, name, value):
        if value is None:
            return qs
        return qs.filter(self.stale_q()) if value else qs.exclude(self.stale_q())

    def filter_subject_type(self, qs, name, value):
        return _subject_type(qs, value)

    def filter_query(self, qs, name, value):
        return _entry_title(qs, value)


class RunFilter(django_filters.FilterSet):
    """Runs: every Celery task execution, with or without a job."""

    job_id = django_filters.UUIDFilter(field_name="job_id")
    kind = CsvFilter(field_name="kind")
    status = CsvFilter(field_name="status", help_text="Run statuses, comma-separated.")
    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains", help_text="Celery task name.")
    without_job = django_filters.BooleanFilter(field_name="job", lookup_expr="isnull")
    subject_id = django_filters.UUIDFilter(field_name="subject_id")
    entry_id = django_filters.UUIDFilter(field_name="entry_id")
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = JobRun
        fields = []
