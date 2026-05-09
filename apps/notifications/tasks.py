from celery import shared_task


@shared_task
def send_notification(notification_type: str, recipient_user_id: str, context: dict):
    from apps.core.models import User
    from apps.notifications.services import NotificationService

    recipient_user = User.objects.get(pk=recipient_user_id)

    NotificationService.send(
        notification_type=notification_type,
        recipient_user=recipient_user,
        context=context,
    )
