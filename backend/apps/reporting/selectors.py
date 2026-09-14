"""Read-only queries over the operational tables.

No new models and no denormalised report tables: everything a store asks for
is already in the ledger and the sales tables, and a second copy of the truth
is a second thing that can be wrong. If a query ever gets too slow for the data
volume, the fix is an index or a materialised view - not a parallel schema.

Margins use the `unit_cost` frozen on each sale line, so a report about last
month does not change when this month's purchases move the average cost.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from apps.cash.models import CashSession
from apps.catalog.models import ProductVariant
from apps.core.money import money
from apps.expenses.models import Expense
from apps.inventory.models import StockLevel
from apps.sales.models import Payment, Refund, Sale, SaleItem

MONEY = DecimalField(max_digits=18, decimal_places=2)
ZERO = Decimal("0.00")


def resolve_period(date_from=None, date_to=None, default_days: int = 30):
    """Default to the last 30 days, inclusive of today."""
    now = timezone.now()
    date_to = date_to or now
    date_from = date_from or (date_to - timedelta(days=default_days))
    return date_from, date_to


def _settled_sales(date_from, date_to, location=None):
    queryset = Sale.objects.filter(
        status__in=Sale.SETTLED_STATUSES, occurred_at__gte=date_from, occurred_at__lte=date_to
    )
    if location is not None:
        queryset = queryset.filter(location=location)
    return queryset


def sales_summary(*, date_from, date_to, location=None) -> dict:
    sales = _settled_sales(date_from, date_to, location)

    totals = sales.aggregate(
        sales_count=Count("id"),
        gross=Coalesce(Sum("total"), ZERO, output_field=MONEY),
        tax=Coalesce(Sum("tax_total"), ZERO, output_field=MONEY),
        discounts=Coalesce(Sum("discount_total"), ZERO, output_field=MONEY),
        refunded=Coalesce(Sum("refunded_total"), ZERO, output_field=MONEY),
    )

    by_method = {
        row["method"]: money(row["total"])
        for row in Payment.objects.filter(sale__in=sales).values("method").annotate(total=Sum("amount"))
    }

    by_day = [
        {
            "date": row["day"],
            "sales_count": row["sales_count"],
            "total": money(row["total"]),
        }
        for row in sales.annotate(day=TruncDate("occurred_at"))
        .values("day")
        .annotate(sales_count=Count("id"), total=Sum("total"))
        .order_by("day")
    ]

    gross = money(totals["gross"])
    refunded = money(totals["refunded"])
    return {
        "period": {"start": date_from, "end": date_to},
        "sales_count": totals["sales_count"],
        "gross_total": gross,
        "refunded_total": refunded,
        "net_total": money(gross - refunded),
        "tax_total": money(totals["tax"]),
        "discount_total": money(totals["discounts"]),
        "average_ticket": money(gross / totals["sales_count"]) if totals["sales_count"] else ZERO,
        "payments_by_method": by_method,
        "by_day": by_day,
    }


def top_products(*, date_from, date_to, location=None, limit: int = 10) -> list[dict]:
    """Ranked by net units sold: returns are subtracted, not ignored."""
    net_quantity = ExpressionWrapper(F("quantity") - F("refunded_quantity"), output_field=MONEY)

    rows = (
        SaleItem.objects.filter(sale__in=_settled_sales(date_from, date_to, location))
        .values("variant_id", "sku", "description")
        .annotate(
            units=Coalesce(Sum(net_quantity), ZERO, output_field=MONEY),
            revenue=Coalesce(Sum("line_total"), ZERO, output_field=MONEY),
        )
        .order_by("-units")[:limit]
    )
    return [
        {
            "variant": str(row["variant_id"]),
            "sku": row["sku"],
            "description": row["description"],
            "units": int(row["units"]),
            "revenue": money(row["revenue"]),
        }
        for row in rows
    ]


def margin_report(*, date_from, date_to, location=None) -> dict:
    """Revenue against the cost frozen on each line when it was sold."""
    items = SaleItem.objects.filter(sale__in=_settled_sales(date_from, date_to, location))

    net_quantity = F("quantity") - F("refunded_quantity")
    revenue_per_unit = ExpressionWrapper(F("line_total") / F("quantity"), output_field=MONEY)

    totals = items.aggregate(
        revenue=Coalesce(
            Sum(ExpressionWrapper(revenue_per_unit * net_quantity, output_field=MONEY)),
            ZERO,
            output_field=MONEY,
        ),
        cost=Coalesce(
            Sum(ExpressionWrapper(F("unit_cost") * net_quantity, output_field=MONEY)),
            ZERO,
            output_field=MONEY,
        ),
        units=Coalesce(Sum(net_quantity), 0),
        units_with_cost=Coalesce(
            Sum(net_quantity, filter=Q(unit_cost__gt=0)),
            0,
        ),
    )

    revenue = money(totals["revenue"])
    cost = money(totals["cost"])
    profit = money(revenue - cost)
    units = int(totals["units"] or 0)
    units_with_cost = int(totals["units_with_cost"] or 0)
    return {
        "period": {"start": date_from, "end": date_to},
        "units_sold": units,
        "revenue": revenue,
        "cost": cost,
        "gross_profit": profit,
        "margin_percent": money(profit / revenue * 100) if revenue else ZERO,
        "cost_coverage": {
            "units_with_cost": units_with_cost,
            "units_total": units,
            "percent": money(Decimal(units_with_cost) / Decimal(units) * 100) if units else ZERO,
            "incomplete": units_with_cost < units,
        },
    }


def inventory_valuation(*, location=None) -> dict:
    """What the stock on hand is worth, at moving average cost."""
    levels = StockLevel.objects.filter(quantity__gt=0).select_related("variant")
    if location is not None:
        levels = levels.filter(location=location)

    totals = levels.aggregate(
        units=Coalesce(Sum("quantity"), 0),
        cost_value=Coalesce(
            Sum(
                ExpressionWrapper(F("quantity") * F("variant__average_cost"), output_field=MONEY)
            ),
            ZERO,
            output_field=MONEY,
        ),
        retail_value=Coalesce(
            Sum(ExpressionWrapper(F("quantity") * F("variant__price"), output_field=MONEY)),
            ZERO,
            output_field=MONEY,
        ),
        units_with_cost=Coalesce(
            Sum("quantity", filter=Q(variant__average_cost__gt=0)),
            0,
        ),
    )

    negative = list(
        StockLevel.objects.filter(quantity__lt=0)
        .select_related("variant", "variant__product", "location")
        .values("variant_id", "variant__sku", "location__name", "quantity")[:50]
    )

    units = int(totals["units"] or 0)
    units_with_cost = int(totals["units_with_cost"] or 0)
    from apps.catalog.selectors import pending_cost_count

    return {
        "units_on_hand": units,
        "cost_value": money(totals["cost_value"]),
        "retail_value": money(totals["retail_value"]),
        "potential_margin": money(totals["retail_value"] - totals["cost_value"]),
        "variants_tracked": ProductVariant.objects.filter(is_active=True).count(),
        # Negative stock only arises from replayed offline sales (decision D4).
        "negative_stock": [
            {
                "variant": str(row["variant_id"]),
                "sku": row["variant__sku"],
                "location": row["location__name"],
                "quantity": row["quantity"],
            }
            for row in negative
        ],
        "cost_coverage": {
            "units_with_cost": units_with_cost,
            "units_total": units,
            "percent": money(Decimal(units_with_cost) / Decimal(units) * 100) if units else ZERO,
            "incomplete": units_with_cost < units,
            "variants_pending_cost": pending_cost_count(),
        },
    }


def cash_sessions_report(*, date_from, date_to, location=None) -> dict:
    """Closed shifts and their differences - the arqueo history."""
    sessions = CashSession.objects.filter(
        status=CashSession.Status.CLOSED, closed_at__gte=date_from, closed_at__lte=date_to
    ).select_related("register", "opened_by", "closed_by")
    if location is not None:
        sessions = sessions.filter(register__location=location)

    totals = sessions.aggregate(
        sessions=Count("id"),
        shortfall=Coalesce(Sum("difference", filter=Q(difference__lt=0)), ZERO, output_field=MONEY),
        surplus=Coalesce(Sum("difference", filter=Q(difference__gt=0)), ZERO, output_field=MONEY),
    )

    return {
        "period": {"start": date_from, "end": date_to},
        "sessions_closed": totals["sessions"],
        "total_shortfall": money(totals["shortfall"]),
        "total_surplus": money(totals["surplus"]),
        "sessions": [
            {
                "id": str(session.pk),
                "register": session.register.name,
                "opened_at": session.opened_at,
                "closed_at": session.closed_at,
                "expected": session.expected_amount,
                "counted": session.counted_amount,
                "difference": session.difference,
                "closed_by": getattr(session.closed_by, "email", None),
            }
            for session in sessions.order_by("-closed_at")[:100]
        ],
    }


def expenses_summary(*, date_from, date_to, location=None) -> dict:
    """Operating spend for the period, broken down the way the owner groups it.

    Merchandise is not here: it enters as a Purchase and is counted as cost of
    goods sold only when it is actually sold, so nothing is double counted.
    """
    expenses = Expense.objects.filter(occurred_at__gte=date_from, occurred_at__lte=date_to)
    if location is not None:
        expenses = expenses.filter(location=location)

    totals = expenses.aggregate(
        count=Count("id"),
        total=Coalesce(Sum("amount"), ZERO, output_field=MONEY),
        from_drawer=Coalesce(
            Sum("amount", filter=Q(cash_session__isnull=False)), ZERO, output_field=MONEY
        ),
    )

    by_category = [
        {
            "category": str(row["category_id"]),
            "name": row["category__name"],
            "total": money(row["total"]),
        }
        for row in expenses.values("category_id", "category__name")
        .annotate(total=Sum("amount"))
        .order_by("-total")
    ]

    by_method = {
        row["payment_method"]: money(row["total"])
        for row in expenses.values("payment_method").annotate(total=Sum("amount"))
    }

    by_day = [
        {"date": row["day"], "total": money(row["total"])}
        for row in expenses.annotate(day=TruncDate("occurred_at"))
        .values("day")
        .annotate(total=Sum("amount"))
        .order_by("day")
    ]

    return {
        "period": {"start": date_from, "end": date_to},
        "expenses_count": totals["count"],
        "expenses_total": money(totals["total"]),
        "paid_from_drawer": money(totals["from_drawer"]),
        "by_category": by_category,
        "by_method": by_method,
        "by_day": by_day,
    }


def profit_and_loss(*, date_from, date_to, location=None) -> dict:
    """The full line, from what came in to what is actually left.

    Revenue is net of refunds; cost is the one frozen on each sale line; the
    expenses are the operating ones. This is the only report in the module
    that answers "how much did I really make", and the only one that can,
    because it is the only one that knows what the business spent.
    """
    margin = margin_report(date_from=date_from, date_to=date_to, location=location)
    expenses = expenses_summary(date_from=date_from, date_to=date_to, location=location)

    gross_profit = margin["gross_profit"]
    expenses_total = expenses["expenses_total"]
    net_profit = money(gross_profit - expenses_total)

    return {
        "period": {"start": date_from, "end": date_to},
        "revenue": margin["revenue"],
        "cost_of_goods": margin["cost"],
        "gross_profit": gross_profit,
        "expenses_total": expenses_total,
        "expenses_by_category": expenses["by_category"],
        "net_profit": net_profit,
        "net_margin_percent": money(net_profit / margin["revenue"] * 100)
        if margin["revenue"]
        else ZERO,
        "cost_coverage": margin["cost_coverage"],
    }


def refunds_summary(*, date_from, date_to, location=None) -> dict:
    refunds = Refund.objects.filter(occurred_at__gte=date_from, occurred_at__lte=date_to)
    if location is not None:
        refunds = refunds.filter(location=location)

    totals = refunds.aggregate(
        count=Count("id"),
        total=Coalesce(Sum("total"), ZERO, output_field=MONEY),
        restocked=Count("id", filter=Q(restock=True)),
    )
    return {
        "period": {"start": date_from, "end": date_to},
        "refunds_count": totals["count"],
        "refunds_total": money(totals["total"]),
        "restocked_count": totals["restocked"],
        "written_off_count": totals["count"] - totals["restocked"],
    }


def dashboard(*, date_from, date_to, location=None, top_limit: int = 5) -> dict:
    """Everything the reports landing page shows, in one round trip.

    The page needs figures that all share one period and one location; fetching
    them one endpoint at a time makes the client stitch a consistent picture
    out of separate answers, and shows the user a page that fills in pieces.
    Each block keeps the exact shape of its own endpoint, so drilling into the
    detailed report means no second payload to learn.
    """
    inventory = inventory_valuation(location=location)

    return {
        "period": {"start": date_from, "end": date_to},
        "sales": sales_summary(date_from=date_from, date_to=date_to, location=location),
        "profit": profit_and_loss(date_from=date_from, date_to=date_to, location=location),
        "refunds": refunds_summary(date_from=date_from, date_to=date_to, location=location),
        "inventory": {
            "units_on_hand": inventory["units_on_hand"],
            "cost_value": inventory["cost_value"],
            "retail_value": inventory["retail_value"],
            "negative_stock_count": len(inventory["negative_stock"]),
            "cost_coverage": inventory["cost_coverage"],
        },
        "top_products": top_products(
            date_from=date_from, date_to=date_to, location=location, limit=top_limit
        ),
    }
