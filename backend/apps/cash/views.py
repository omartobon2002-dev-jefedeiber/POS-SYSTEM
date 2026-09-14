from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core import capabilities as caps
from apps.core.audit import record_audit
from apps.core.views import TenantModelViewSet, TenantViewSetMixin

from .models import CashMovement, CashMovementType, CashRegister, CashSession
from .serializers import (
    CashMovementInputSerializer,
    CashMovementSerializer,
    CashRegisterSerializer,
    CashSessionSerializer,
    CloseSessionSerializer,
    OpenSessionSerializer,
    TransferSessionResultSerializer,
    TransferSessionSerializer,
)
from .services import CashService


class CashRegisterViewSet(TenantModelViewSet):
    serializer_class = CashRegisterSerializer
    model = CashRegister
    select_related = ("location",)
    read_capability = caps.CASH_READ
    write_capability = caps.ORGANIZATION_MANAGE
    filterset_fields = ["is_active"]
    search_fields = ["name", "code"]

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])


class CashSessionViewSet(
    TenantViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Shifts are opened and closed through actions, never edited directly."""

    serializer_class = CashSessionSerializer
    model = CashSession
    select_related = ("register", "opened_by", "closed_by", "previous_session", "superseded_by")
    read_capability = caps.CASH_READ
    write_capability = caps.CASH_OPEN
    capability_overrides = {
        "close": caps.CASH_CLOSE,
        "transfer": caps.CASH_CLOSE,
        "movements": caps.CASH_MOVEMENT,
    }
    filterset_fields = ["status", "register"]
    ordering_fields = ["opened_at", "closed_at"]

    @extend_schema(request=OpenSessionSerializer, responses={201: CashSessionSerializer})
    def create(self, request):
        serializer = OpenSessionSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        session = CashService.open_session(
            organization=request.organization,
            register=serializer.validated_data["register"],
            user=request.user,
            opening_amount=serializer.validated_data["opening_amount"],
            notes=serializer.validated_data.get("notes", ""),
        )
        return Response(CashSessionSerializer(session).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=CloseSessionSerializer, responses={200: CashSessionSerializer})
    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        serializer = CloseSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = CashService.close_session(
            session=self.get_object(),
            counted_amount=serializer.validated_data["counted_amount"],
            user=request.user,
            notes=serializer.validated_data.get("notes", ""),
        )
        return Response(CashSessionSerializer(session).data)

    @extend_schema(
        request=TransferSessionSerializer,
        responses={200: TransferSessionResultSerializer},
    )
    @action(detail=True, methods=["post"])
    def transfer(self, request, pk=None):
        """Hand the open drawer to another cashier (same-day continuation)."""
        serializer = TransferSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        closed, opened = CashService.transfer_session(
            session=self.get_object(),
            counted_amount=serializer.validated_data["counted_amount"],
            from_user=request.user,
            to_user=serializer.validated_data["to_user"],
            notes=serializer.validated_data.get("notes", ""),
        )
        return Response(
            {
                "closed_session": CashSessionSerializer(closed).data,
                "opened_session": CashSessionSerializer(opened).data,
            }
        )

    @extend_schema(responses={200: None})
    @action(detail=True, methods=["get"])
    def summary(self, request, pk=None):
        """Arqueo view: expected cash plus totals by movement type and payment method."""
        return Response(CashService.session_summary(self.get_object()))

    @extend_schema(
        request=CashMovementInputSerializer,
        responses={201: CashMovementSerializer},
        methods=["POST"],
    )
    @action(detail=True, methods=["get", "post"])
    def movements(self, request, pk=None):
        session = self.get_object()
        if request.method == "GET":
            queryset = session.movements.select_related("created_by").all()
            page = self.paginate_queryset(queryset)
            serializer = CashMovementSerializer(page if page is not None else queryset, many=True)
            if page is not None:
                return self.get_paginated_response(serializer.data)
            return Response(serializer.data)

        serializer = CashMovementInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        amount = data["amount"]
        if data["movement_type"] == CashMovementType.WITHDRAWAL:
            amount = -amount

        movement = CashService.record_movement(
            session=session,
            movement_type=data["movement_type"],
            amount=amount,
            user=request.user,
            note=data.get("note", ""),
        )
        record_audit(
            organization=request.organization,
            action=f"cash.{data['movement_type'].lower()}",
            actor=request.user,
            obj=movement,
            metadata={"amount": str(movement.amount), "note": movement.note},
        )
        return Response(CashMovementSerializer(movement).data, status=status.HTTP_201_CREATED)


class CashMovementViewSet(
    TenantViewSetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = CashMovementSerializer
    model = CashMovement
    select_related = ("session", "created_by")
    read_capability = caps.CASH_READ
    filterset_fields = ["session", "movement_type"]
    ordering_fields = ["created_at"]
