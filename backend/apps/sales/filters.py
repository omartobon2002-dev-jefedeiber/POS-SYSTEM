from __future__ import annotations

import django_filters as filters
from django.db.models import Exists, OuterRef

from apps.core.enums import PaymentMethod

from .models import Payment, Refund, Sale


class SaleFilter(filters.FilterSet):
    location = filters.UUIDFilter(field_name="location_id")
    customer = filters.UUIDFilter(field_name="customer_id")
    seller = filters.UUIDFilter(field_name="seller_id")
    cash_session = filters.UUIDFilter(field_name="cash_session_id")
    occurred_after = filters.IsoDateTimeFilter(field_name="occurred_at", lookup_expr="gte")
    occurred_before = filters.IsoDateTimeFilter(field_name="occurred_at", lookup_expr="lte")
    # Sales with at least one payment of this method. An EXISTS, not a join, so
    # it does not interfere with the list's item/payment aggregates.
    payment_method = filters.ChoiceFilter(
        choices=PaymentMethod.choices, method="filter_payment_method"
    )

    class Meta:
        model = Sale
        fields = ["status", "source"]

    def filter_payment_method(self, queryset, name, value):
        return queryset.filter(
            Exists(Payment.objects.filter(sale_id=OuterRef("pk"), method=value))
        )


class RefundFilter(filters.FilterSet):
    sale = filters.UUIDFilter(field_name="sale_id")
    location = filters.UUIDFilter(field_name="location_id")
    occurred_after = filters.IsoDateTimeFilter(field_name="occurred_at", lookup_expr="gte")
    occurred_before = filters.IsoDateTimeFilter(field_name="occurred_at", lookup_expr="lte")

    class Meta:
        model = Refund
        fields = ["method", "restock"]
