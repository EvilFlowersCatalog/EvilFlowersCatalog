from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class AssistantUserPolicy(BaseModel):
    """Per-user moderation state and quota overrides for the assistant.

    IP-015 D2/D11: the reference implementation carried `blocked*` columns on
    its mirrored users table. That table is not recreated, and `core.User` only
    has `is_active` — which would lock a user out of the entire catalog rather
    than just the chat. Assistant-specific moderation therefore lives here.

    A missing row means "default policy": not blocked, limits from settings.
    """

    class Meta:
        app_label = "assistant"
        db_table = "assistant_user_policies"
        default_permissions = ()
        verbose_name = _("Assistant user policy")
        verbose_name_plural = _("Assistant user policies")

    user = models.OneToOneField("core.User", on_delete=models.CASCADE, related_name="assistant_policy")

    is_blocked = models.BooleanField(default=False)
    blocked_until = models.DateTimeField(null=True, blank=True)
    blocked_reason = models.CharField(max_length=500, blank=True, default="")

    #: NULL means "use the configured default".
    daily_message_limit = models.IntegerField(null=True, blank=True)
    daily_token_limit = models.IntegerField(null=True, blank=True)

    @property
    def blocked(self) -> bool:
        """True while the block is in force.

        A block with `blocked_until` in the past has expired and is reported as
        lifted, so a temporary block needs no scheduled job to clear it.
        """
        if not self.is_blocked:
            return False
        if self.blocked_until is not None and self.blocked_until <= timezone.now():
            return False
        return True

    def __str__(self):
        return f"{self.user_id}: {'blocked' if self.blocked else 'ok'}"
