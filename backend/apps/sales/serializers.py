from __future__ import annotations

from rest_framework import serializers

from apps.cash.models import CashRegister
from apps.catalog.models import ProductVariant
from apps.core.enums import PaymentMethod
from apps.core.fields import TenantPrimaryKeyRelatedField
from apps.core.serializers import TenantModelSerializer
from apps.customers.models import Customer
from apps.organizations.models import Location

from .labels import seller_label
from .models import Payment, Refund, RefundItem, Sale, SaleItem


class SaleItemSerializer(TenantModelSerializer):
    refundable_quantity = serializers.IntegerField(read_only=True)

    class Meta:
        model = SaleItem
        fields = [
            "id",
            "variant",
            "sku",
            "description",
            "quantity",
            "unit_price",
            "discount_amount",
            "line_total",
            "tax_rate",
            "taxable_base",
            "tax_amount",
            "refunded_quantity",
            "refundable_quantity",
        ]
        read_only_fields = fields


class PaymentSerializer(TenantModelSerializer):
    class Meta:
        model = Payment
        fields = ["id", "method", "amount", "reference", "created_at"]
        read_only_fields = fields


class RefundItemSerializer(TenantModelSerializer):
    description = serializers.CharField(source="sale_item.description", read_only=True)

    class Meta:
        model = RefundItem
        fields = ["id", "sale_item", "description", "quantity", "amount"]
        read_only_fields = fields


class RefundSerializer(TenantModelSerializer):
    items = RefundItemSerializer(many=True, read_only=True)
    sale_number = serializers.CharField(source="sale.number", read_only=True)

    class Meta:
        model = Refund
        fields = [
            "id",
            "number",
            "sale",
            "sale_number",
            "location",
            "cash_session",
            "total",
            "method",
            "restock",
            "reason",
            "occurred_at",
            "created_by",
            "items",
            "created_at",
        ]
        read_only_fields = fields


class SaleListSerializer(TenantModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True, default=None)
    seller_email = serializers.CharField(source="seller.email", read_only=True, default=None)
    seller_name = serializers.SerializerMethodField()
    item_count = serializers.IntegerField(read_only=True)
    # Distinct methods only, not per-method amounts - enough to render a
    # payment-method badge in a list without the N+1 of fetching each sale's
    # full payment breakdown. Amounts still require GET /sales/{id}/.
    payment_methods = serializers.ListField(child=serializers.CharField(), read_only=True)

    class Meta:
        model = Sale
        fields = [
            "id",
            "number",
            "status",
            "location",
            "customer",
            "customer_name",
            "seller",
            "seller_email",
            "seller_name",
            "total",
            "refunded_total",
            "item_count",
            "payment_methods",
            "occurred_at",
        ]
        read_only_fields = fields

    def get_seller_name(self, obj) -> str:
        return seller_label(obj)


class SaleSerializer(TenantModelSerializer):
    items = SaleItemSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    refunds = RefundSerializer(many=True, read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True, default=None)
    seller_name = serializers.SerializerMethodField()
    net_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = Sale
        fields = [
            "id",
            "number",
            "status",
            "location",
            "customer",
            "customer_name",
            "seller",
            "seller_name",
            "cash_session",
            "subtotal",
            "discount_total",
            "tax_total",
            "total",
            "paid_total",
            "change_amount",
            "refunded_total",
            "net_total",
            "source",
            "device_id",
            "occurred_at",
            "notes",
            "cancelled_at",
            "cancellation_reason",
            "items",
            "payments",
            "refunds",
            "created_at",
        ]
        read_only_fields = fields

    def get_seller_name(self, obj) -> str:
        return seller_label(obj)


class SaleLineInputSerializer(serializers.Serializer):
    variant = TenantPrimaryKeyRelatedField(queryset=ProductVariant.objects)
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        allow_null=True,
        help_text="Overrides the catalogue price. Omit to use the shelf price.",
    )
    discount_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, default=0, min_value=0
    )


class PaymentInputSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    reference = serializers.CharField(max_length=80, required=False, allow_blank=True)


class SaleCreateSerializer(serializers.Serializer):
    """A complete sale in one call. Totals are always recomputed server-side."""

    id = serializers.UUIDField(
        required=False,
        help_text="Optional client-generated id, so an offline terminal keeps the id it printed.",
    )
    location = TenantPrimaryKeyRelatedField(queryset=Location.objects, required=False)
    customer = TenantPrimaryKeyRelatedField(queryset=Customer.objects, required=True, allow_null=False)
    cash_register = TenantPrimaryKeyRelatedField(
        queryset=CashRegister.objects, required=False, allow_null=True
    )
    occurred_at = serializers.DateTimeField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True)
    expected_total = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        allow_null=True,
        help_text="If sent and it disagrees with the server total, the sale is rejected (409).",
    )
    lines = SaleLineInputSerializer(many=True)
    payments = PaymentInputSerializer(many=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("A sale needs at least one item.")
        return value

    def validate_payments(self, value):
        if not value:
            raise serializers.ValidationError("A sale needs at least one payment.")
        return value


class SaleCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=240, required=False, allow_blank=True)


class RefundLineInputSerializer(serializers.Serializer):
    sale_item = TenantPrimaryKeyRelatedField(queryset=SaleItem.objects)
    quantity = serializers.IntegerField(min_value=1)


class RefundCreateSerializer(serializers.Serializer):
    sale = TenantPrimaryKeyRelatedField(queryset=Sale.objects)
    method = serializers.ChoiceField(choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    restock = serializers.BooleanField(default=True)
    reason = serializers.CharField(max_length=240, required=False, allow_blank=True)
    cash_register = TenantPrimaryKeyRelatedField(
        queryset=CashRegister.objects, required=False, allow_null=True
    )
    occurred_at = serializers.DateTimeField(required=False)
    lines = RefundLineInputSerializer(many=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("A refund needs at least one line.")
        return value
