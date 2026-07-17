from celery import shared_task


@shared_task
def send_notification(
    notification_type: str,
    recipient_user_id: str,
    context: dict,
    attachment_refs: list | None = None,
):
    from apps.core.models import User
    from apps.notifications.attachments import resolve_attachments
    from apps.notifications.services import NotificationService

    recipient_user = User.objects.get(pk=recipient_user_id)

    # Materialise attachments here in the worker — refs (not bytes) crossed the
    # Celery boundary, and the fetch (e.g. a fresh .lcpl) belongs at send time.
    attachments = resolve_attachments(attachment_refs)

    NotificationService.send(
        notification_type=notification_type,
        recipient_user=recipient_user,
        context=context,
        attachments=attachments,
    )
