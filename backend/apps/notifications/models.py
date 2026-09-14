"""Web Push subscriptions for sale alerts to owners/managers."""
from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.models import TenantScopedModel


class WebPushSubscription(TenantScopedModel):
    """Browser push endpoint for one device belonging to a member of the shop."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="web_push_subscriptions",
    )
    endpoint = models.URLField(max_length=500)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)

    class Meta:
        db_table = "web_push_subscriptions"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["endpoint"], name="uq_web_push_endpoint"),
        ]
        indexes = [
            models.Index(fields=["organization", "user"]),
        ]

    def __str__(self) -> str:
        return f"push:{self.user_id}@{self.organization_id}"
