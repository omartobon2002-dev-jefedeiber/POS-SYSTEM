from __future__ import annotations

import django_filters as filters
from django.db.models import F

from .models import InventoryMovement, StockDiscrepancy, StockLevel


class UUIDInFilter(filters.BaseInFilter, filters.UUIDFilter):
    """`?products=<uuid>,<uuid>` - several ids in one query parameter."""


class StockLevelFilter(filters.FilterSet):
    location = filters.UUIDFilter(field_name="location_id")
    variant = filters.UUIDFilter(field_name="variant_id")
    product = filters.UUIDFilter(field_name="variant__product_id")
    # The stock of just the products on screen (a POS page, a catalogue page)
    # instead of every balance row of the business.
    products = UUIDInFilter(field_name="variant__product_id", lookup_expr="in")
    # The flag lives on the variant, not on the balance row, but it is spelled
    # `is_active` here so the "show me the deleted ones" escape hatch reads the
    # same on every endpoint.
    is_active = filters.BooleanFilter(field_name="variant__is_active")
    below_reorder_point = filters.BooleanFilter(method="filter_below_reorder_point")
    in_stock = filters.BooleanFilter(method="filter_in_stock")

    class Meta:
        model = StockLevel
        fields = ["location", "variant", "product"]

    def filter_below_reorder_point(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(quantity__lte=F("reorder_point"))

    def filter_in_stock(self, queryset, name, value):
        return queryset.filter(quantity__gt=0) if value else queryset.filter(quantity__lte=0)


class InventoryMovementFilter(filters.FilterSet):
    location = filters.UUIDFilter(field_name="location_id")
    variant = filters.UUIDFilter(field_name="variant_id")
    product = filters.UUIDFilter(field_name="variant__product_id")
    occurred_after = filters.IsoDateTimeFilter(field_name="occurred_at", lookup_expr="gte")
    occurred_before = filters.IsoDateTimeFilter(field_name="occurred_at", lookup_expr="lte")

    class Meta:
        model = InventoryMovement
        fields = ["location", "variant", "product", "movement_type", "source_type", "source_id"]


class StockDiscrepancyFilter(filters.FilterSet):
    location = filters.UUIDFilter(field_name="location_id")
    variant = filters.UUIDFilter(field_name="variant_id")

    class Meta:
        model = StockDiscrepancy
        fields = ["is_resolved"]
