# Generated by Django 4.0.5 on 2022-06-25 10:34

from django.db import migrations, models
import django.db.models.deletion
import uuid


def create_database_source(apps, schema_editor):
    AuthSource = apps.get_model("core", "AuthSource")
    User = apps.get_model("core", "User")

    auth_source = AuthSource.objects.create(
        name="Database", driver="database", is_active=True
    )

    for user in User.objects.all():
        user.auth_source = auth_source
        user.save()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0013_iso_languages"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuthSource",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, primary_key=True, serialize=False
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("name", models.CharField(max_length=200)),
                (
                    "driver",
                    models.CharField(
                        choices=[("database", "database"), ("ldap", "ldap")],
                        max_length=20,
                    ),
                ),
                ("content", models.JSONField(null=True)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={
                "verbose_name": "Authentication source",
                "verbose_name_plural": "Authentication sources",
                "db_table": "auth_sources",
                "default_permissions": (),
            },
        ),
        migrations.AddField(
            model_name="user",
            name="auth_source",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="core.authsource",
            ),
        ),
        migrations.RunPython(create_database_source),
        migrations.AlterField(
            model_name="user",
            name="auth_source",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="core.authsource"
            ),
        ),
    ]
