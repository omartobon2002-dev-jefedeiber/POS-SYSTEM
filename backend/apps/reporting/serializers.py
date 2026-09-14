"""Response shapes for the reports.

Declared explicitly rather than returning bare dicts so the OpenAPI schema is
usable by a generated client - a report nobody can type against is a report the
frontend will re-derive by hand.
"""
from __future__ import annotations

from rest_framework import serializers


class PeriodSerializer(serializers.Serializer):
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()


class MoneyField(serializers.DecimalField):
    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 18)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("read_only", True)
        super().__init__(**kwargs)


class DailySalesSerializer(serializers.Serializer):
    date = serializers.DateField()
    sales_count = serializers.IntegerField()
    total = MoneyField()


class SalesSummarySerializer(serializers.Serializer):
    period = PeriodSerializer()
    sales_count = serializers.IntegerField()
    gross_total = MoneyField()
    refunded_total = MoneyField()
    net_total = MoneyField()
    tax_total = MoneyField()
    discount_total = MoneyField()
    average_ticket = MoneyField()
    payments_by_method = serializers.DictField(child=MoneyField())
    by_day = DailySalesSerializer(many=True)


class TopProductSerializer(serializers.Serializer):
    variant = serializers.UUIDField()
    sku = serializers.CharField()
    description = serializers.CharField()
    units = serializers.IntegerField()
    revenue = MoneyField()


class CostCoverageSerializer(serializers.Serializer):
    """How much of the margin/valuation is backed by a real unit cost."""

    units_with_cost = serializers.IntegerField()
    units_total = serializers.IntegerField()
    percent = MoneyField()
    incomplete = serializers.BooleanField()
    variants_pending_cost = serializers.IntegerField(required=False)


class MarginSerializer(serializers.Serializer):
    period = PeriodSerializer()
    units_sold = serializers.IntegerField()
    revenue = MoneyField()
    cost = MoneyField()
    gross_profit = MoneyField()
    margin_percent = MoneyField()
    cost_coverage = CostCoverageSerializer()


class NegativeStockSerializer(serializers.Serializer):
    variant = serializers.UUIDField()
    sku = serializers.CharField()
    location = serializers.CharField()
    quantity = serializers.IntegerField()


class InventoryValuationSerializer(serializers.Serializer):
    units_on_hand = serializers.IntegerField()
    cost_value = MoneyField()
    retail_value = MoneyField()
    potential_margin = MoneyField()
    variants_tracked = serializers.IntegerField()
    negative_stock = NegativeStockSerializer(many=True)
    cost_coverage = CostCoverageSerializer()


class CashSessionRowSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    register = serializers.CharField()
    opened_at = serializers.DateTimeField()
    closed_at = serializers.DateTimeField()
    expected = MoneyField()
    counted = MoneyField()
    difference = MoneyField()
    closed_by = serializers.CharField(allow_null=True)


class CashSessionsReportSerializer(serializers.Serializer):
    period = PeriodSerializer()
    sessions_closed = serializers.IntegerField()
    total_shortfall = MoneyField()
    total_surplus = MoneyField()
    sessions = CashSessionRowSerializer(many=True)


class RefundsSummarySerializer(serializers.Serializer):
    period = PeriodSerializer()
    refunds_count = serializers.IntegerField()
    refunds_total = MoneyField()
    restocked_count = serializers.IntegerField()
    written_off_count = serializers.IntegerField()


class ExpenseCategoryTotalSerializer(serializers.Serializer):
    category = serializers.UUIDField()
    name = serializers.CharField()
    total = MoneyField()


class DailyTotalSerializer(serializers.Serializer):
    date = serializers.DateField()
    total = MoneyField()


class ExpensesSummarySerializer(serializers.Serializer):
    period = PeriodSerializer()
    expenses_count = serializers.IntegerField()
    expenses_total = MoneyField()
    paid_from_drawer = MoneyField()
    by_category = ExpenseCategoryTotalSerializer(many=True)
    by_method = serializers.DictField(child=MoneyField())
    by_day = DailyTotalSerializer(many=True)


class ProfitSerializer(serializers.Serializer):
    period = PeriodSerializer()
    revenue = MoneyField()
    cost_of_goods = MoneyField()
    gross_profit = MoneyField()
    expenses_total = MoneyField()
    expenses_by_category = ExpenseCategoryTotalSerializer(many=True)
    net_profit = MoneyField()
    net_margin_percent = MoneyField()
    cost_coverage = CostCoverageSerializer()


class DashboardInventorySerializer(serializers.Serializer):
    units_on_hand = serializers.IntegerField()
    cost_value = MoneyField()
    retail_value = MoneyField()
    negative_stock_count = serializers.IntegerField()
    cost_coverage = CostCoverageSerializer()


class DashboardSerializer(serializers.Serializer):
    """The whole reports landing page in one payload."""

    period = PeriodSerializer()
    sales = SalesSummarySerializer()
    profit = ProfitSerializer()
    refunds = RefundsSummarySerializer()
    inventory = DashboardInventorySerializer()
    top_products = TopProductSerializer(many=True)


class ReportIndexSerializer(serializers.Serializer):
    reports = serializers.ListField(child=serializers.CharField())
