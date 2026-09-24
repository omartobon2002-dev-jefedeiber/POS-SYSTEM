"""Purchase receipt: the only way stock legitimately enters from a supplier."""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.exceptions import InvalidOperation
from apps.core.models import DocumentSequence
from apps.core.money import money
from apps.core.sequences import next_document_number
from apps.inventory.models import MovementType
from apps.inventory.services import InventoryService, MovementLine

from .models import Purchase


@transaction.atomic
def create_purchase(*, organization, location, supplier=None, items, user=None, **fields) -> Purchase:
    """Create a purchase in DRAFT. Nothing touches inventory yet."""
    if not items:
        raise InvalidOperation("A purchase needs at least one item.")

    purchase = Purchase.objects.create(
        organization=organization,
        location=location,
        supplier=supplier,
        purchased_at=fields.get("purchased_at") or timezone.now(),
        supplier_invoice=fields.get("supplier_invoice", ""),
        notes=fields.get("notes", ""),
        created_by=user,
    )

    total = Decimal("0.00")
    from .models import PurchaseItem

    rows = []
    for item in items:
        line_total = money(Decimal(item["quantity"]) * Decimal(item["unit_cost"]))
        total += line_total
        rows.append(
            PurchaseItem(
                organization=organization,
                purchase=purchase,
                variant=item["variant"],
                quantity=item["quantity"],
                unit_cost=Decimal(item["unit_cost"]),
                total_cost=line_total,
            )
        )
    PurchaseItem.objects.bulk_create(rows)

    purchase.total_cost = money(total)
    purchase.save(update_fields=["total_cost", "updated_at"])

    record_audit(
        organization=organization,
        action="purchase.created",
        actor=user,
        obj=purchase,
        metadata={
            "supplier": str(supplier.pk) if supplier else None,
            "total_cost": str(purchase.total_cost),
        },
    )
    return purchase


@transaction.atomic
def receive_purchase(*, purchase: Purchase, user=None, received_at=None) -> Purchase:
    """Move a DRAFT purchase into stock.

    Inventory movements, average cost and the purchase number are all written
    in one transaction: a received purchase whose stock never arrived, or stock
    with no purchase behind it, must be impossible.
    """
    # Locked so two concurrent receipts of the same draft cannot both see
    # DRAFT and write the stock twice.
    purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)
    if purchase.status != Purchase.Status.DRAFT:
        raise InvalidOperation(
            f"Only a draft purchase can be received (this one is {purchase.status}).",
            purchase=str(purchase.pk),
        )

    items = list(purchase.items.select_related("variant").all())
    if not items:
        raise InvalidOperation("A purchase with no items cannot be received.")

    received_at = received_at or timezone.now()

    InventoryService.apply_movements(
        organization=purchase.organization,
        location=purchase.location,
        lines=[
            MovementLine(
                variant_id=str(item.variant_id),
                quantity=item.quantity,
                unit_cost=item.unit_cost,
            )
            for item in items
        ],
        movement_type=MovementType.PURCHASE,
        user=user,
        source_type="purchase",
        source_id=str(purchase.pk),
        occurred_at=received_at,
    )

    # Average cost is updated after the movements, so the helper sees the
    # post-receipt balance and derives the previous one by subtraction.
    for item in items:
        InventoryService.update_average_cost(
            variant=item.variant,
            incoming_quantity=item.quantity,
            unit_cost=item.unit_cost,
        )

    purchase.status = Purchase.Status.RECEIVED
    purchase.received_at = received_at
    purchase.received_by = user
    purchase.number = next_document_number(
        organization=purchase.organization,
        location=purchase.location,
        document_type=DocumentSequence.DocumentType.PURCHASE,
    )
    purchase.save(update_fields=["status", "received_at", "received_by", "number", "updated_at"])

    record_audit(
        organization=purchase.organization,
        action="purchase.received",
        actor=user,
        obj=purchase,
        metadata={
            "number": purchase.number,
            "units": sum(item.quantity for item in items),
            "total_cost": str(purchase.total_cost),
        },
    )
    return purchase


@transaction.atomic
def cancel_purchase(*, purchase: Purchase, user=None, reason: str = "") -> Purchase:
    """Cancel a draft. A received purchase is reversed with an adjustment, not cancelled."""
    purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)
    if purchase.status != Purchase.Status.DRAFT:
        raise InvalidOperation(
            "Only a draft purchase can be cancelled. A received purchase must be "
            "corrected with an inventory adjustment so the ledger keeps its history.",
            purchase=str(purchase.pk),
        )
    purchase.status = Purchase.Status.CANCELLED
    purchase.save(update_fields=["status", "updated_at"])
    record_audit(
        organization=purchase.organization,
        action="purchase.cancelled",
        actor=user,
        obj=purchase,
        metadata={"reason": reason},
    )
    return purchase
