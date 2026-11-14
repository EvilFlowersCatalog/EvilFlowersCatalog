"""IP-031 rev. 2: the declarative `Job` aggregate; `JobProtocol` becomes `JobRun` (same table)."""

import uuid

import django.db.models.deletion
import django.db.models.functions.datetime
from django.conf import settings
from django.db import migrations, models

#: Run-only kinds at the time of this migration: their runs get no job (many runs per subject).
_RUN_ONLY = ("notifications.send", "core.embed_entries", "dataverse.resume_workflow")


def create_jobs(apps, schema_editor):
    """One job per kind and subject from the runs recorded so far, without intent (nothing to reconcile)."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO jobs (id, created_at, updated_at, kind, subject_type_id, subject_id, catalog_id, entry_id,
                              spec, desired_state, state, reason, detail, generation, observed_generation,
                              attempts, current_run_id, last_transition_at)
            SELECT gen_random_uuid(), now(), now(), r.kind, r.subject_type_id, r.subject_id, r.catalog_id,
                   r.entry_id, '{}'::jsonb, 'completed',
                   CASE r.status
                       WHEN 'queued' THEN 'queued' WHEN 'received' THEN 'queued'
                       WHEN 'started' THEN 'running' WHEN 'retrying' THEN 'running'
                       WHEN 'succeeded' THEN 'succeeded' WHEN 'revoked' THEN 'cancelled'
                       ELSE 'failed' END,
                   CASE WHEN r.status IN ('failed', 'lost', 'rejected') THEN r.error END,
                   '{}'::jsonb, 0, 0, 0, r.id, COALESCE(r.last_event_at, r.updated_at)
            FROM (
                SELECT DISTINCT ON (kind, subject_type_id, subject_id) *
                FROM job_protocols
                WHERE kind IS NOT NULL AND NOT (kind = ANY(%s))
                ORDER BY kind, subject_type_id, subject_id, created_at DESC
            ) r
            """,
            [list(_RUN_ONLY)],
        )
        cursor.execute("""
            UPDATE job_protocols r SET job_id = j.id
            FROM jobs j
            WHERE r.kind = j.kind
              AND r.subject_type_id IS NOT DISTINCT FROM j.subject_type_id
              AND r.subject_id IS NOT DISTINCT FROM j.subject_id
            """)
        # A service's follow-up tasks belong to their parent's job.
        cursor.execute("""
            UPDATE job_protocols c SET job_id = p.job_id
            FROM job_protocols p
            WHERE c.parent_id = p.id AND c.job_id IS NULL AND p.job_id IS NOT NULL
            """)


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("core", "0050_entry_cover_hashes"),
        ("tasks", "0002_jobs_ledger"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveIndex(model_name="jobevent", name="job_event_job_ts_idx"),
        migrations.RenameModel(old_name="JobProtocol", new_name="JobRun"),
        migrations.AlterModelOptions(
            name="jobrun",
            options={
                "default_permissions": ("view", "add"),
                "permissions": (("report_job", "Can report the outcome of a background job"),),
                "verbose_name": "Job run",
                "verbose_name_plural": "Job runs",
            },
        ),
        migrations.RenameField(model_name="jobevent", old_name="job", new_name="run"),
        migrations.RemoveField(model_name="jobrun", name="options"),
        migrations.RemoveField(model_name="jobrun", name="retry_of"),
        migrations.RemoveField(model_name="jobrun", name="requested_by"),
        migrations.RemoveField(model_name="jobrun", name="revoke_requested_at"),
        migrations.CreateModel(
            name="Job",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, db_default=django.db.models.functions.datetime.Now()),
                ),
                ("kind", models.CharField(db_index=True, max_length=100, null=True)),
                ("subject_id", models.UUIDField(null=True)),
                ("spec", models.JSONField(default=dict)),
                (
                    "desired_state",
                    models.CharField(
                        choices=[("completed", "completed"), ("cancelled", "cancelled")],
                        default="completed",
                        max_length=20,
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("pending", "pending"),
                            ("blocked", "blocked"),
                            ("queued", "queued"),
                            ("running", "running"),
                            ("cancelling", "cancelling"),
                            ("succeeded", "succeeded"),
                            ("failed", "failed"),
                            ("cancelled", "cancelled"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("reason", models.TextField(null=True)),
                ("detail", models.JSONField(default=dict)),
                ("generation", models.PositiveIntegerField(default=0)),
                ("observed_generation", models.PositiveIntegerField(default=0)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("last_transition_at", models.DateTimeField(null=True)),
                (
                    "catalog",
                    models.ForeignKey(
                        null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="core.catalog"
                    ),
                ),
                (
                    "entry",
                    models.ForeignKey(
                        null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="core.entry"
                    ),
                ),
                (
                    "subject_type",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "cause",
                    models.ForeignKey(
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="consequences",
                        to="tasks.job",
                    ),
                ),
                (
                    "current_run",
                    models.ForeignKey(
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="tasks.jobrun",
                    ),
                ),
            ],
            options={
                "verbose_name": "Job",
                "verbose_name_plural": "Jobs",
                "db_table": "jobs",
                "default_permissions": ("view",),
                "indexes": [
                    models.Index(fields=["state", "kind"], name="job_state_kind_idx"),
                    models.Index(fields=["catalog", "kind", "state"], name="job_catalog_kind_state_idx"),
                    models.Index(fields=["entry"], name="job_entry_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("kind", "subject_type", "subject_id"),
                        name="job_kind_subject_unique",
                        nulls_distinct=False,
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="jobrun",
            name="job",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="runs",
                to="tasks.job",
            ),
        ),
        migrations.AddField(
            model_name="jobevent",
            name="job_ref",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="events",
                to="tasks.job",
            ),
        ),
        migrations.AlterField(
            model_name="jobevent",
            name="run",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="events",
                to="tasks.jobrun",
            ),
        ),
        migrations.AlterField(model_name="jobevent", name="type", field=models.CharField(max_length=40)),
        migrations.AddIndex(
            model_name="jobevent", index=models.Index(fields=["run", "timestamp"], name="job_event_job_ts_idx")
        ),
        migrations.AddIndex(
            model_name="jobevent",
            index=models.Index(fields=["job_ref", "timestamp"], name="job_event_job_ref_ts_idx"),
        ),
        migrations.RunPython(create_jobs, migrations.RunPython.noop),
    ]
