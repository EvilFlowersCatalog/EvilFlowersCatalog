"""#169: workers no longer report over HTTP, so `tasks.report_job` goes (with its grants)."""

from django.db import migrations


def drop_permission(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    Permission.objects.filter(content_type__app_label="tasks", codename="report_job").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("tasks", "0003_jobs_aggregate"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="jobrun",
            options={
                "default_permissions": ("view", "add"),
                "verbose_name": "Job run",
                "verbose_name_plural": "Job runs",
            },
        ),
        migrations.RunPython(drop_permission, migrations.RunPython.noop),
    ]
