from __future__ import annotations

from rest_framework import serializers

from apps.catalog.models import ProductVariant
from apps.core.costs import can_see_costs
from apps.core.fields import TenantPrimaryKeyRelatedField
from apps.core.serializers import TenantModelSerializer
from apps.organizations.models import Location

from .models import InventoryMovement, StockDiscrepancy, StockLevel


class StockLevelSerializer(TenantModelSerializer):
    variant_sku = serializers.CharField(source="variant.sku", read_only=True)
    variant_name = serializers.CharField(source="variant.display_name", read_only=True)
    location_name = serializers.CharField(source="location.name", read_only=True)

    class Meta:
        model = StockLevel
        fields = [
            "id",
            "location",
            "location_name",
            "variant",
            "variant_sku",
            "variant_name",
            "quantity",
            "reorder_point",
            "updated_at",
        ]
        read_only_fields = ["id", "location", "variant", "quantity", "updated_at"]


class InventoryMovementSerializer(TenantModelSerializer):
    variant_sku = serializers.CharField(source="variant.sku", read_only=True)
    variant_name = serializers.CharField(source="variant.display_name", read_only=True)
    created_by_email = serializers.CharField(source="created_by.email", read_only=True, default=None)

    class Meta:
        model = InventoryMovement
        fields = [
            "id",
            "location",
            "variant",
            "variant_sku",
            "variant_name",
            "quantity",
            "movement_type",
            "unit_cost",
            "source_type",
            "source_id",
            "created_by",
            "created_by_email",
            "occurred_at",
            "note",
            "created_at",
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not can_see_costs(self.context.get("request")):
            data.pop("unit_cost", None)
        return data


class StockDiscrepancySerializer(TenantModelSerializer):
    variant_name = serializers.CharField(source="variant.display_name", read_only=True)

    class Meta:
        model = StockDiscrepancy
        fields = [
            "id",
            "location",
            "variant",
            "variant_name",
            "quantity_before",
            "quantity_requested",
            "quantity_after",
            "source_type",
            "source_id",
            "reason",
            "is_resolved",
            "resolved_at",
            "created_at",
        ]
        read_only_fields = [f for f in fields if f != "is_resolved"]


class MovementLineSerializer(serializers.Serializer):
    variant = TenantPrimaryKeyRelatedField(queryset=ProductVariant.objects)
    quantity = serializers.IntegerField()
    unit_cost = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    note = serializers.CharField(max_length=240, required=False, allow_blank=True)

    def validate_quantity(self, value):
        if value == 0:
            raise serializers.ValidationError("Quantity cannot be zero.")
        return value


class InventoryOperationSerializer(serializers.Serializer):
    """Shared payload for manual adjustments and initial stock loads."""

    location = TenantPrimaryKeyRelatedField(queryset=Location.objects, required=False)
    reason = serializers.CharField(max_length=240, required=False, allow_blank=True)
    lines = MovementLineSerializer(many=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("At least one line is required.")
        seen = set()
        for line in value:
            variant_id = str(line["variant"].pk)
            if variant_id in seen:
                raise serializers.ValidationError("Each variant may appear only once per operation.")
            seen.add(variant_id)
        return value


class InitialStockSerializer(InventoryOperationSerializer):
    def validate_lines(self, value):
        value = super().validate_lines(value)
        for line in value:
            if line["quantity"] <= 0:
                raise serializers.ValidationError("Initial stock quantities must be positive.")
        return value
