from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.response import Response

from apps.core import capabilities as caps
from apps.core.permissions import HasCapability, HasOrganization

from .models import Subscription
from .serializers import SubscriptionSerializer


class CurrentSubscriptionViewSet(viewsets.ViewSet):
    permission_classes = [HasOrganization, HasCapability]
    read_capability = caps.SUBSCRIPTION_READ
    write_capability = caps.SUBSCRIPTION_MANAGE
    serializer_class = SubscriptionSerializer

    @extend_schema(responses={200: SubscriptionSerializer})
    def list(self, request):
        subscription = (
            Subscription.objects.select_related("plan").filter(organization=request.organization).first()
        )
        if subscription is None:
            return Response({"detail": "No subscription for this organization.", "code": "not_found"}, 404)
        return Response(SubscriptionSerializer(subscription).data)
