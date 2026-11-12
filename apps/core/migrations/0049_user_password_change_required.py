from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0048_entry_reviews")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="password_change_required",
            field=models.BooleanField(default=False),
        ),
    ]
