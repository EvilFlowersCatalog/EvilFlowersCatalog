"""IP-030: failed-turn rows, model seconds, feedback and study on messages; section summaries replace chapters.

Chapter summaries (IP-024 §7) were keyed by table-of-contents chapters, which 2 of 435 publications have. Each
one is copied onto the section of the publication's active extraction run whose heading matches the chapter
title (case and accents ignored); one without such a section is dropped and is generated again on demand.
"""

import unicodedata

import django.db.models.deletion
import django.db.models.functions.datetime
import uuid
from django.db import migrations, models


def _fold(text):
    text = unicodedata.normalize("NFKD", text or "")
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


def copy_chapter_summaries(apps, schema_editor):
    ChapterSummary = apps.get_model("assistant", "ChapterSummary")
    SectionSummary = apps.get_model("assistant", "SectionSummary")
    ExtractionRun = apps.get_model("extraction", "ExtractionRun")
    Paragraph = apps.get_model("extraction", "Paragraph")

    for chapter in ChapterSummary.objects.all():
        run = ExtractionRun.objects.filter(acquisition__entry_id=chapter.entry_id, is_active=True).first()
        if run is None:
            continue
        wanted = _fold(chapter.title)
        sections = Paragraph.objects.filter(run=run).exclude(section=None).values_list("section", flat=True)
        match = next((section for section in sections.distinct() if _fold(section) == wanted), None)
        if match is None:
            continue
        pages = Paragraph.objects.filter(run=run, section=match).aggregate(
            first=models.Min("page"), last=models.Max("page")
        )
        SectionSummary.objects.get_or_create(
            run=run,
            section=match,
            defaults={
                "entry_id": chapter.entry_id,
                "first_page": pages["first"] or chapter.page,
                "last_page": pages["last"] or chapter.end_page,
                "summary": chapter.summary,
                # Not the digest of the section text: the next request for the section re-generates it.
                "digest": "",
                "model": chapter.model,
                "tokens_used": chapter.tokens_used,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("assistant", "0009_chat_history_start"),
        ("core", "0050_entry_cover_hashes"),
        ("extraction", "0006_ip030_region_vectors_references"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatmessage",
            name="error",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="feedback",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="model_seconds",
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="study",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.RemoveField(
            model_name="assistantuserpolicy",
            name="daily_token_limit",
        ),
        migrations.AddField(
            model_name="assistantuserpolicy",
            name="daily_seconds_limit",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="SectionSummary",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, db_default=django.db.models.functions.datetime.Now()),
                ),
                ("section", models.TextField()),
                ("first_page", models.PositiveIntegerField()),
                ("last_page", models.PositiveIntegerField()),
                ("summary", models.TextField()),
                ("concepts", models.JSONField(blank=True, default=list)),
                ("digest", models.CharField(max_length=64)),
                ("model", models.CharField(blank=True, default="", max_length=100)),
                ("tokens_used", models.IntegerField(default=0)),
                (
                    "entry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="section_summaries",
                        to="core.entry",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="section_summaries",
                        to="extraction.extractionrun",
                    ),
                ),
            ],
            options={
                "verbose_name": "Section summary",
                "verbose_name_plural": "Section summaries",
                "db_table": "assistant_section_summary",
                "ordering": ["entry", "first_page"],
                "default_permissions": (),
                "constraints": [
                    models.UniqueConstraint(fields=("run", "section"), name="assistant_section_summary_unique")
                ],
            },
        ),
        migrations.RunPython(copy_chapter_summaries, migrations.RunPython.noop),
        migrations.DeleteModel(name="ChapterSummary"),
    ]
