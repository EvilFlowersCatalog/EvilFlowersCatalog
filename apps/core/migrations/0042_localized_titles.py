import re

from django.conf import settings
from django.db import migrations, models

DEFAULT_LANGUAGE = settings.EVILFLOWERS_DEFAULT_LANGUAGE
assert re.match(r"^[a-z]{2}$", DEFAULT_LANGUAGE), "EVILFLOWERS_DEFAULT_LANGUAGE must be an ISO 639-1 code"


def to_localized_sql(table: str, column: str) -> str:
    # Existing single-language texts become {"<default language>": "<text>"}, empty ones {}
    return f"""
        ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT;
        ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE jsonb USING (
            CASE WHEN "{column}" IS NULL OR btrim("{column}") = '' THEN '{{}}'::jsonb
            ELSE jsonb_build_object('{DEFAULT_LANGUAGE}', "{column}") END
        );
        ALTER TABLE "{table}" ALTER COLUMN "{column}" SET NOT NULL;
    """


def from_localized_sql(table: str, column: str, max_length: int, nullable: bool) -> str:
    # Keep the default language translation (or any other one when it is missing)
    # (subqueries are not allowed in USING, hence jsonb_path_query_first)
    value = f"""COALESCE(
        "{column}" ->> '{DEFAULT_LANGUAGE}',
        jsonb_path_query_first("{column}", '$.*') #>> '{{}}'
    )"""
    if not nullable:
        value = f"COALESCE({value}, '')"
    return f"""
        ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE varchar({max_length}) USING left({value}, {max_length});
        ALTER TABLE "{table}" ALTER COLUMN "{column}" {"DROP" if nullable else "SET"} NOT NULL;
    """


def mark_translatable(apps, schema_editor):
    Language = apps.get_model("core", "Language")
    Language.objects.filter(alpha2__in={DEFAULT_LANGUAGE, "en"}).update(translatable=True)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0041_useractivity"),
    ]

    operations = [
        migrations.AddField(
            model_name="language",
            name="translatable",
            field=models.BooleanField(
                default=False, help_text="Feed titles and category labels can be entered in this language"
            ),
        ),
        migrations.RunPython(mark_translatable, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name="category",
            name="categories_label_4454dd_idx",
        ),
        migrations.RemoveIndex(
            model_name="category",
            name="categories_catalog_0f30ce_idx",
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    to_localized_sql("categories", "label"),
                    from_localized_sql("categories", "label", 255, nullable=True),
                ),
                migrations.RunSQL(
                    to_localized_sql("feeds", "title"),
                    from_localized_sql("feeds", "title", 100, nullable=False),
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="category",
                    name="label",
                    field=models.JSONField(blank=True, default=dict),
                ),
                migrations.AlterField(
                    model_name="feed",
                    name="title",
                    field=models.JSONField(default=dict),
                ),
            ],
        ),
    ]
