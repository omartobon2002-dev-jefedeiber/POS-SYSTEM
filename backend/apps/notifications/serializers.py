from __future__ import annotations

from rest_framework import serializers

from .models import WebPushSubscription


class WebPushSubscriptionSerializer(serializers.Serializer):
    endpoint = serializers.URLField(max_length=500)
    keys = serializers.DictField(child=serializers.CharField(), required=False)
    p256dh = serializers.CharField(max_length=200, required=False, allow_blank=True)
    auth = serializers.CharField(max_length=100, required=False, allow_blank=True)

    def validate(self, attrs):
        keys = attrs.pop("keys", None) or {}
        p256dh = attrs.get("p256dh") or keys.get("p256dh")
        auth = attrs.get("auth") or keys.get("auth")
        if not p256dh or not auth:
            raise serializers.ValidationError(
                {"keys": "p256dh and auth are required (PushSubscription.keys)."}
            )
        attrs["p256dh"] = p256dh
        attrs["auth"] = auth
        return attrs


class WebPushSubscriptionResponseSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebPushSubscription
        fields = ("id", "endpoint", "created_at")
