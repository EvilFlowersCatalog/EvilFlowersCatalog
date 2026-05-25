# IP-011: three new reservation UX notification types
# (reservation_promoted, reservation_cancelled, reservation_claim_reminder).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0002_alter_notificationlog_notification_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notificationlog",
            name="notification_type",
            field=models.CharField(
                choices=[
                    ("license_created", "License Created"),
                    ("license_renewed", "License Renewed"),
                    ("license_returned", "License Returned"),
                    ("license_revoked", "License Revoked"),
                    ("license_expiring_soon", "License Expiring Soon"),
                    ("reservation_placed", "Reservation Placed"),
                    ("reservation_available", "Reservation Available"),
                    ("reservation_expired", "Reservation Expired"),
                    ("reservation_promoted", "Reservation Promoted"),
                    ("reservation_cancelled", "Reservation Cancelled"),
                    ("reservation_claim_reminder", "Reservation Claim Reminder"),
                    ("passphrase_changed", "Passphrase Changed"),
                ],
                max_length=50,
            ),
        ),
    ]
