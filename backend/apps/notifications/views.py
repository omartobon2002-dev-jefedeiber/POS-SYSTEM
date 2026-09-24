from __future__ import annotations

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework.permissions import BasePermission

from apps.core.permissions import HasOrganization, SubscriptionGrantsAccess

from . import services
from .models import WebPushSubscription
from .serializers import WebPushSubscriptionResponseSerializer, WebPushSubscriptionSerializer


class CanReceiveSalePush(BasePermission):
    """Only OWNER / organization.manage may manage sale-alert subscriptions."""

    message = "Only the owner can enable sale notifications."

    def has_permission(self, request, view):
        membership = getattr(request, "membership", None)
        if membership is None:
            return False
        return services.can_receive_sale_push(membership)


class VapidPublicKeyView(APIView):
    permission_classes = [HasOrganization, CanReceiveSalePush, SubscriptionGrantsAccess]

    @extend_schema(responses={200: None})
    def get(self, request):
        public = getattr(settings, "VAPID_PUBLIC_KEY", "") or ""
        if not public:
            return Response(
                {
                    "detail": "Web Push is not configured on this server.",
                    "code": "vapid_not_configured",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"public_key": public})


class WebPushSubscriptionView(APIView):
    permission_classes = [HasOrganization, CanReceiveSalePush, SubscriptionGrantsAccess]

    @extend_schema(
        request=WebPushSubscriptionSerializer,
        responses={201: WebPushSubscriptionResponseSerializer},
    )
    def post(self, request):
        serializer = WebPushSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # The endpoint is unique platform-wide (one browser, one endpoint), but
        # the same owner may subscribe from two businesses: look it up unscoped
        # so the row moves to the current business instead of colliding.
        sub, _created = WebPushSubscription.all_objects.update_or_create(
            endpoint=data["endpoint"],
            defaults={
                "organization": request.organization,
                "user": request.user,
                "p256dh": data["p256dh"],
                "auth": data["auth"],
            },
        )
        return Response(
            WebPushSubscriptionResponseSerializer(sub).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(request=WebPushSubscriptionSerializer, responses={204: None})
    def delete(self, request):
        endpoint = request.data.get("endpoint") if isinstance(request.data, dict) else None
        if not endpoint:
            return Response(
                {"detail": "endpoint is required.", "code": "endpoint_required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        deleted, _ = WebPushSubscription.objects.filter(
            organization=request.organization,
            user=request.user,
            endpoint=endpoint,
        ).delete()
        if not deleted:
            # Idempotent: already gone is fine.
            pass
        return Response(status=status.HTTP_204_NO_CONTENT)
