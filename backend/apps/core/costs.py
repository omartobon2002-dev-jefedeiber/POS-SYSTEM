"""Cost visibility helpers.

`costs.read` is the gate for average/purchase/movement unit costs. Cashiers may
write products and stock without ever seeing those fields; the server strips
them on read and ignores them on write.
"""
from __future__ import annotations

from apps.core import capabilities as caps

COST_RESPONSE_FIELDS = frozenset(
    {
        "average_cost",
        "last_purchase_cost",
        "unit_cost",
        "cost",
        "total_cost",
        "cost_value",
        "cost_of_goods",
        "potential_margin",
        "gross_profit",
        "margin_percent",
        "net_margin_percent",
    }
)


def can_see_costs(request) -> bool:
    membership = getattr(request, "membership", None) if request is not None else None
    if membership is None:
        return False
    return membership.has_capability(caps.COSTS_READ)


def strip_cost_fields(data):
    """Remove cost keys from a dict or nested list/dict structure."""
    if isinstance(data, list):
        return [strip_cost_fields(item) for item in data]
    if not isinstance(data, dict):
        return data
    return {
        key: strip_cost_fields(value)
        for key, value in data.items()
        if key not in COST_RESPONSE_FIELDS
    }
