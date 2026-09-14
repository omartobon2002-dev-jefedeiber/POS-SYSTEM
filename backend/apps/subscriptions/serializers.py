from __future__ import annotations

from rest_framework import serializers

from .models import Subscription


class SubscriptionSerializer(serializers.ModelSerializer):
    is_usable = serializers.BooleanField(read_only=True)
    grants_access = serializers.BooleanField(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            "id",
            "status",
            "billing_cycle",
            "trial_ends_at",
            "current_period_start",
            "current_period_end",
            "is_usable",
            "grants_access",
        ]
