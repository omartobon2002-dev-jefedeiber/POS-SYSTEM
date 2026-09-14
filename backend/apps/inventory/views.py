from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.response import Response

from apps.core import capabilities as caps
from apps.core.audit import record_audit
from apps.core.views import (
    ActiveByDefaultMixin,
    IdempotentActionMixin,
    TenantReadOnlyViewSet,
    TenantViewSetMixin,
)
from apps.organizations.selectors import default_location

from .filters import InventoryMovementFilter, StockDiscrepancyFilter, StockLevelFilter
from .models import InventoryMovement, MovementType, StockDiscrepancy, StockLevel
from .serializers import (
    InitialStockSerializer,
    InventoryMovementSerializer,
    InventoryOperationSerializer,
    StockDiscrepancySerializer,
    StockLevelSerializer,
)
from .services import MovementLine, record_adjustment, record_initial_stock


class StockLevelViewSet(
    ActiveByDefaultMixin,
    TenantViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Read the materialised balances. Quantity is never writable here.

    Stock only changes through movements; allowing a PATCH on quantity would
    reintroduce exactly the untraceable `stock -= n` this design removes.
    Only `reorder_point` is editable.

    A deleted product leaves its balance row behind at zero - the ledger is
    rebuilt from it - but the row should not keep showing up in the stock list
    as if the product still existed, so rows of deactivated variants are hidden
    unless asked for.
    """

    active_field = "variant__is_active"
    serializer_class = StockLevelSerializer
    model = StockLevel
    select_related = ("variant", "variant__product", "location")
    read_capability = caps.INVENTORY_READ
    write_capability = caps.INVENTORY_ADJUST
    filterset_class = StockLevelFilter
    search_fields = ["variant__sku", "variant__barcode", "variant__product__name"]
    ordering_fields = ["quantity", "updated_at"]


class InventoryMovementViewSet(TenantReadOnlyViewSet):
    """The ledger. Append-only, so there is no write endpoint by design."""

    serializer_class = InventoryMovementSerializer
    model = InventoryMovement
    select_related = ("variant", "variant__product", "location", "created_by")
    read_capability = caps.INVENTORY_READ
    filterset_class = InventoryMovementFilter
    search_fields = ["variant__sku", "note"]
    ordering_fields = ["occurred_at", "created_at"]


class StockDiscrepancyViewSet(
    TenantViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Negative-stock events raised by offline operations (decision D4)."""

    serializer_class = StockDiscrepancySerializer
    model = StockDiscrepancy
    select_related = ("variant", "location")
    read_capability = caps.INVENTORY_READ
    write_capability = caps.INVENTORY_ADJUST
    filterset_class = StockDiscrepancyFilter

    def perform_update(self, serializer):
        from django.utils import timezone

        instance = serializer.save()
        if instance.is_resolved and instance.resolved_at is None:
            instance.resolved_at = timezone.now()
            instance.save(update_fields=["resolved_at", "updated_at"])


class _InventoryWriteViewSet(IdempotentActionMixin, TenantViewSetMixin, viewsets.ViewSet):
    """Manual inventory writes: initial load and adjustments.

    Both accept an optional `Idempotency-Key` header; a retried request after a
    network timeout replays the original result instead of double-counting
    stock.
    """

    read_capability = caps.INVENTORY_READ
    write_capability = caps.INVENTORY_ADJUST

    def _apply(self, request, serializer_class, movement_type):
        serializer = serializer_class(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        location = data.get("location") or default_location()
        if location is None:
            return Response(
                {"detail": "This organization has no active location.", "code": "no_location"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Cashiers can load stock without costs.read; never let a forged
        # unit_cost reach the ledger or average_cost in that case.
        allow_cost = request.membership.has_capability(caps.COSTS_READ)
        lines = [
            MovementLine(
                variant_id=str(line["variant"].pk),
                quantity=line["quantity"],
                unit_cost=line.get("unit_cost") if allow_cost else None,
                note=line.get("note", ""),
            )
            for line in data["lines"]
        ]

        recorder = record_initial_stock if movement_type == MovementType.INITIAL_STOCK else record_adjustment
        kwargs = {
            "organization": request.organization,
            "location": location,
            "lines": lines,
            "user": request.user,
        }
        if movement_type == MovementType.INITIAL_STOCK:
            kwargs["note"] = data.get("reason") or "Initial stock"
        else:
            kwargs["reason"] = data.get("reason", "")

        movements = recorder(**kwargs)

        is_initial = movement_type == MovementType.INITIAL_STOCK
        record_audit(
            organization=request.organization,
            action="inventory.initial_stock" if is_initial else "inventory.adjusted",
            actor=request.user,
            object_type="Location",
            object_id=str(location.pk),
            metadata={
                "reason": data.get("reason", ""),
                "lines": [{"variant": line.variant_id, "quantity": line.quantity} for line in lines],
            },
        )

        return Response(
            InventoryMovementSerializer(movements, many=True, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class InitialStockViewSet(_InventoryWriteViewSet):
    """Loads opening balances when a business starts using the system."""

    @extend_schema(request=InitialStockSerializer, responses={201: InventoryMovementSerializer(many=True)})
    def create(self, request):
        return self.run_idempotent(
            request,
            endpoint="inventory.initial_stock",
            handler=lambda: self._apply(request, InitialStockSerializer, MovementType.INITIAL_STOCK),
        )


class InventoryAdjustmentViewSet(_InventoryWriteViewSet):
    """Counted-stock corrections, damages, losses. Quantities may be negative."""

    @extend_schema(
        request=InventoryOperationSerializer, responses={201: InventoryMovementSerializer(many=True)}
    )
    def create(self, request):
        return self.run_idempotent(
            request,
            endpoint="inventory.adjustment",
            handler=lambda: self._apply(request, InventoryOperationSerializer, MovementType.ADJUSTMENT),
        )
