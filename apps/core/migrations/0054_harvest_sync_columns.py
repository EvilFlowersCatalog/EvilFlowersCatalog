"""IP-033 / IP-034: catalog OAI exposure, entry origin, edition and withdrawal.

`origin = 'harvest:dataverse'` for entries the Dataverse sync imported (they carry `dataverse_pid`): the origin of
the Dataverse source (slug `dataverse`, IP-034). IP-033 Q7 keeps entries of sources flagged as OAI providers out of
OAI-PMH, while Elvira books mirrored *to* Dataverse keep `elvira` and stay harvestable. The identifiers validator
now reads its keys at call time (`IdentifierKeysValidator`), so deployments may add external systems.
"""

import apps.core.validators
import django.contrib.postgres.fields.hstore
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0053_read_events"),
    ]

    operations = [
        migrations.AddField(
            model_name="catalog",
            name="harvestable",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="entry",
            name="edition",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="origin",
            field=models.CharField(db_index=True, default="elvira", max_length=80),
        ),
        migrations.AddField(
            model_name="entry",
            name="withdrawn_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AlterField(
            model_name="entry",
            name="identifiers",
            field=django.contrib.postgres.fields.hstore.HStoreField(
                null=True, validators=[apps.core.validators.IdentifierKeysValidator()]
            ),
        ),
        migrations.RunSQL(
            "UPDATE entries SET origin = 'harvest:dataverse' WHERE identifiers ? 'dataverse_pid'",
            "UPDATE entries SET origin = 'elvira' WHERE origin = 'harvest:dataverse'",
        ),
    ]
