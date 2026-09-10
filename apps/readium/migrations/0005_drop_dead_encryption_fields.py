from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("readium", "0004_reservation"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="encryptedcontent",
            name="content_key_encrypted",
        ),
        migrations.AlterField(
            model_name="encryptedcontent",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending Encryption"),
                    ("completed", "Encryption Completed"),
                    ("failed", "Encryption Failed"),
                    ("registered", "Registered with LCP Server"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
