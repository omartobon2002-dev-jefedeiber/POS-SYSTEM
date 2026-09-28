from __future__ import annotations

import django_filters as filters
from django.db.models import F as models_F
from django.db.models import IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from .models import Category, Product, ProductVariant

# Every filter on a foreign key is declared explicitly. django-filter resolves
# generated relational filters by calling `Model._default_manager.all()` at
# import time, which is exactly when no tenant context exists. Declaring them
# keeps the strict TenantManager strict.


class CategoryFilter(filters.FilterSet):
    parent = filters.UUIDFilter(field_name="parent_id")
    root_only = filters.BooleanFilter(field_name="parent_id", lookup_expr="isnull")

    class Meta:
        model = Category
        fields = ["is_active"]


class ProductFilter(filters.FilterSet):
    category = filters.UUIDFilter(field_name="category_id")
    brand = filters.UUIDFilter(field_name="brand_id")
    # `?stock_status=low|out` across the whole catalogue, optionally at one
    # location (`?stock_location=<uuid>`). Before this the catalogue screen
    # could only filter the page it had already loaded.
    #   out: tracked product with no units left (active variants).
    #   low: some units left, but no more than the sum of its reorder points.
    stock_status = filters.ChoiceFilter(
        choices=[("low", "Low stock"), ("out", "Out of stock")], method="filter_stock_status"
    )
    stock_location = filters.UUIDFilter(method="filter_noop")

    class Meta:
        model = Product
        fields = ["is_active"]

    def filter_noop(self, queryset, name, value):
        return queryset

    def filter_stock_status(self, queryset, name, value):
        from apps.inventory.models import StockLevel

        levels = StockLevel.objects.filter(variant__product=OuterRef("pk"), variant__is_active=True)
        location = self.form.cleaned_data.get("stock_location")
        if location:
            levels = levels.filter(location_id=location)

        def total(field):
            # A correlated subquery, not a join: summing through the variants
            # join would multiply with any other join the request adds (search).
            aggregate = levels.values("variant__product").annotate(total=Sum(field)).values("total")
            return Coalesce(Subquery(aggregate, output_field=IntegerField()), Value(0))

        queryset = queryset.filter(track_inventory=True).annotate(
            stock_total=total("quantity"), stock_min=total("reorder_point")
        )
        if value == "out":
            return queryset.filter(stock_total__lte=0)
        return queryset.filter(Q(stock_total__gt=0) & Q(stock_total__lte=models_F("stock_min")))


class ProductVariantFilter(filters.FilterSet):
    product = filters.UUIDFilter(field_name="product_id")
    category = filters.UUIDFilter(field_name="product__category_id")
    brand = filters.UUIDFilter(field_name="product__brand_id")

    class Meta:
        model = ProductVariant
        fields = ["size", "color", "is_active"]
