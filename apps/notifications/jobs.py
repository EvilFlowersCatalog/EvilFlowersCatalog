"""Notification job kind (IP-031): one e-mail to one reader."""

from apps.core.models import User
from apps.tasks.kinds import JobKind, first_arg, registry


@registry.register
class SendNotificationKind(JobKind):
    """Sent by domain events only. A failed one can be re-sent (`retry` re-sends the recorded call);
    a succeeded one is not retried silently: re-sending e-mails a reader twice (#142)."""

    name = "notifications.send"
    title = "Notification e-mail"
    description = "Render a notification template and send it to one reader."
    task = "apps.notifications.tasks.send_notification"
    subject_model = User
    startable = False
    exclusive = False

    def subject_from_call(self, args, kwargs):
        if isinstance(kwargs, dict) and kwargs.get("recipient_user_id"):
            return first_arg([], kwargs, "recipient_user_id")
        return first_arg(list(args or [])[1:], {}, "recipient_user_id")
