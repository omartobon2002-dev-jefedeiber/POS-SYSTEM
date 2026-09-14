"""Catalogue read helpers that do not belong on a model."""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Exists, OuterRef, Sum

from apps.inventory.models import StockLevel
from apps.sales.models import SaleItem

from .models import ProductVariant

ZERO = Decimal("0")


def variants_pending_cost():
    """Active variants with no cost that already matter operationally.

    A variant is pending when average_cost is zero and it either has stock on
    hand or has appeared on a sale line. Pure catalogue stubs with no stock and
    no sales stay quiet so the owner is not flooded on day one.
    """
    has_stock = StockLevel.objects.filter(variant_id=OuterRef("pk"), quantity__gt=0)
    has_sales = SaleItem.objects.filter(variant_id=OuterRef("pk"))
    return (
        ProductVariant.objects.filter(is_active=True, average_cost=ZERO)
        .annotate(on_hand=Sum("stock_levels__quantity"))
        .filter(Exists(has_stock) | Exists(has_sales))
        .select_related("product")
        .order_by("product__name", "sku")
        .distinct()
    )


def pending_cost_count() -> int:
    return variants_pending_cost().count()
