"""Notification job kind (IP-031): run-only — every e-mail is a run of a reader, nothing to declare."""

from apps.core.models import User
from apps.tasks.contract import run_only

run_only(
    "notifications.send",
    "apps.notifications.tasks.send_notification",
    "Notification e-mail",
    "Render a notification template and send it to one reader.",
    subject_model=User,
    subject_arg=("recipient_user_id", 1),
)
