from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0014_auth_source'),
    ]

    operations = [
        migrations.RenameField(
            model_name='user',
            old_name='email',
            new_name='username',
        ),
        migrations.AlterField(
            model_name='user',
            name='username',
            field=models.CharField(max_length=200, unique=True),
        ),
    ]
