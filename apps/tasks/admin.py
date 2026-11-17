"""IP-035: the jobs ledger (IP-031) in the console (Background jobs module), read-only.

Jobs change through the declarative facade (`/api/v1/jobs`, MCP `apply_job`): desired state in, the
reconciler moves the observed state. The console's Jobs page (`console/jobs/`) shows them live.
"""

from django.utils.translation import gettext_lazy as _

from apps.console.options import ConsoleModelAdmin, register
from apps.tasks.models import Job, JobEvent, JobRun


@register(Job, module="jobs")
class JobAdmin(ConsoleModelAdmin):
    list_display = ("kind", "entry", "catalog", "desired_state", "state", "attempts", "last_transition_at")
    list_filter = ("state", "desired_state", "kind")
    search_fields = ("kind", "entry__title", "reason")
    list_select_related = ("entry", "catalog")
    ordering = ("-last_transition_at",)
    attention = ((_("jobs failed"), {"state": "failed"}),)


@register(JobRun, module="jobs")
class JobRunAdmin(ConsoleModelAdmin):
    big = True
    list_display = ("name", "kind", "status", "queue", "worker", "attempts", "runtime", "started_at", "finished_at")
    list_filter = ("status", "queue")
    search_fields = ("trace_id",)
    ordering = ("-created_at",)


@register(JobEvent, module="jobs")
class JobEventAdmin(ConsoleModelAdmin):
    big = True
    list_display = ("type", "run", "job_ref", "hostname", "timestamp")
    list_filter = ("type",)
    ordering = ("-timestamp",)
