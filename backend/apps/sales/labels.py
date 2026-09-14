"""Human-readable labels for sale-related entities (receipts, push, lists)."""
from __future__ import annotations

from apps.accounts.models import Membership


def seller_label(sale) -> str:
    """Name shown on receipts and notifications for the cashier who closed the sale.

    Prefer the user's full name, then the org membership username (cashiers often
    have no email), then email. Never return empty.
    """
    seller = getattr(sale, "seller", None)
    if seller is None:
        return "Alguien"
    name = (getattr(seller, "full_name", None) or "").strip()
    if name:
        return name
    membership = Membership.objects.filter(
        organization_id=sale.organization_id, user_id=seller.pk
    ).first()
    if membership:
        return membership.username
    return getattr(seller, "email", None) or "Alguien"
