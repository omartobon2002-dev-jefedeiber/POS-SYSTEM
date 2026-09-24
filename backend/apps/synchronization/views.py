from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.response import Response

from apps.core import capabilities as caps
from apps.core.audit import record_audit
from apps.core.params import parse_datetime_param, parse_uuid_param
from apps.core.views import TenantModelViewSet, TenantViewSetMixin
from apps.organizations.models import Location
from apps.organizations.selectors import default_location

from .filters import DeviceFilter, SyncOperationFilter
from .models import Device, SyncOperation
from .selectors import issue_device_token
from .serializers import (
    DeviceRegistrationSerializer,
    DeviceSerializer,
    SyncOperationSerializer,
    SyncPullSerializer,
    SyncPushResultSerializer,
    SyncPushSerializer,
)
from .services import SyncService, pull_changes


class DeviceViewSet(TenantModelViewSet):
    """Terminals allowed to push offline operations."""

    serializer_class = DeviceSerializer
    model = Device
    select_related = ("location", "cash_register")
    read_capability = caps.ORGANIZATION_READ
    write_capability = caps.ORGANIZATION_MANAGE
    filterset_class = DeviceFilter
    search_fields = ["name", "identifier"]

    @extend_schema(request=DeviceRegistrationSerializer, responses={200: DeviceSerializer})
    def create(self, request, *args, **kwargs):
        """Register or re-register a terminal.

        Idempotent by identifier: a till that reinstalls the app and registers
        again keeps its device row, and therefore its history.
        """
        serializer = DeviceRegistrationSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        location = data.get("location") or default_location()
        if location is None:
            return Response(
                {"detail": "This organization has no active location.", "code": "no_location"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        device, created = Device.objects.update_or_create(
            organization=request.organization,
            identifier=data["identifier"],
            defaults={
                "name": data["name"],
                "location": location,
                "cash_register": data.get("cash_register"),
                "platform": data.get("platform", ""),
                "app_version": data.get("app_version", ""),
                "is_active": True,
                "registered_by": request.user,
                "last_seen_at": timezone.now(),
            },
        )
        # Rotated on every registration, and returned in the clear only here.
        # The till stores it and presents it as X-Device-Token so its cashiers
        # sign in with a username and password, without naming the business.
        raw_token = issue_device_token(device)
        record_audit(
            organization=request.organization,
            action="device.registered" if created else "device.updated",
            actor=request.user,
            obj=device,
            metadata={"identifier": device.identifier, "name": device.name},
        )
        return Response(
            {**DeviceSerializer(device).data, "token": raw_token},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def perform_destroy(self, instance):
        # Deactivating cuts a lost terminal off without erasing what it sent.
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        record_audit(
            organization=self.request.organization,
            action="device.deactivated",
            actor=self.request.user,
            obj=instance,
        )


class SyncOperationViewSet(
    TenantViewSetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Push offline operations, and read what happened to them."""

    serializer_class = SyncOperationSerializer
    model = SyncOperation
    select_related = ("device",)
    read_capability = caps.INVENTORY_READ
    write_capability = caps.SYNC_PUSH
    filterset_class = SyncOperationFilter
    ordering_fields = ["received_at", "occurred_at"]
    throttle_scope = "sync"

    @extend_schema(
        request=SyncPushSerializer,
        responses={200: SyncPushResultSerializer(many=True)},
    )
    def create(self, request):
        """Replay a batch. Each operation succeeds, fails or is a no-op, independently.

        The batch itself needs no idempotency key: every operation carries its
        own `operation_id`, so resending the whole batch is safe.
        """
        serializer = SyncPushSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        results = SyncService.push(
            organization=request.organization,
            device=serializer.validated_data["device"],
            operations=serializer.validated_data["operations"],
            user=request.user,
            allow_price_override=request.membership.has_capability(caps.SALES_OVERRIDE_PRICE),
        )
        # `accepted` counts work actually done: a duplicate is neither an
        # acceptance nor a failure, it is a no-op the terminal can forget about.
        summary = {
            "accepted": sum(
                1
                for r in results
                if r["status"] == SyncOperation.Status.PROCESSED and not r["duplicate"]
            ),
            "duplicated": sum(1 for r in results if r["duplicate"]),
            "failed": sum(
                1 for r in results if r["status"] == SyncOperation.Status.FAILED and not r["duplicate"]
            ),
        }
        return Response({"summary": summary, "results": results})


class SyncPullViewSet(TenantViewSetMixin, viewsets.ViewSet):
    """The delta a terminal applies to its local database before going offline."""

    read_capability = caps.PRODUCTS_READ
    write_capability = caps.SYNC_PUSH
    serializer_class = SyncPullSerializer
    throttle_scope = "sync"

    @extend_schema(
        parameters=[
            OpenApiParameter("since", str, description="ISO-8601 cursor from a previous pull."),
            OpenApiParameter("location", str, description="Restrict stock levels to one location."),
        ],
        responses={200: SyncPullSerializer},
    )
    def list(self, request):
        since = parse_datetime_param(request.query_params.get("since"), "since")
        location_id = parse_uuid_param(request.query_params.get("location"), "location")
        location = Location.objects.filter(pk=location_id).first() if location_id else None

        changes = pull_changes(organization=request.organization, since=since, location=location)
        return Response(SyncPullSerializer(changes).data)
