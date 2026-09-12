"""InventoryService - the only place stock is allowed to change.

Concurrency policy
------------------
Two registers selling the same variant at the same time must not both read the
same balance. Every mutation locks the StockLevel rows it touches with
SELECT ... FOR UPDATE, in ascending variant id order. The ordering is not
cosmetic: without it, register 1 selling (A, B) and register 2 selling (B, A)
deadlock. Locks are taken on StockLevel, not on ProductVariant, so editing a
product's price never blocks a sale.

Out-of-stock policy (decision D4)
---------------------------------
Online operations refuse to oversell (InsufficientStock, HTTP 409). Operations
replayed from an offline terminal pass allow_negative=True: the goods already
left the shelf, so the movement is recorded and a StockDiscrepancy is opened
for a human to reconcile.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.core.exceptions import InsufficientStock, InvalidOperation

from .models import InventoryMovement, MovementType, StockDiscrepancy, StockLevel


@dataclass(frozen=True)
class MovementLine:
    """One variant's delta inside a single inventory operation."""

    variant_id: str
    quantity: int
    unit_cost: Decimal | None = None
    note: str = ""


class InventoryService:
    @staticmethod
    @transaction.atomic
    def apply_movements(
        *,
        organization,
        location,
        lines: list[MovementLine],
        movement_type: str,
        user=None,
        source_type: str = "",
        source_id: str = "",
        occurred_at=None,
        allow_negative: bool = False,
        reason: str = "",
    ) -> list[InventoryMovement]:
        """Record movements and update the materialised balances atomically."""
        if not lines:
            raise InvalidOperation("An inventory operation needs at least one line.")

        occurred_at = occurred_at or timezone.now()
        deltas: dict[str, int] = {}
        for line in lines:
            if line.quantity == 0:
                raise InvalidOperation("Movement quantity cannot be zero.", variant=str(line.variant_id))
            deltas[str(line.variant_id)] = deltas.get(str(line.variant_id), 0) + line.quantity

        variant_ids = sorted(deltas)
        variants = {
            str(v.pk): v
            for v in ProductVariant.objects.filter(pk__in=variant_ids).select_related("product")
        }
        missing = set(variant_ids) - set(variants)
        if missing:
            # Tenant isolation: a variant from another organization is simply
            # not visible here, so it surfaces as "unknown", never as access.
            raise InvalidOperation("Unknown product variant.", variants=sorted(missing))

        if location.organization_id != organization.pk:
            raise InvalidOperation("Location does not belong to this organization.")

        levels = InventoryService._lock_levels(organization, location, variant_ids)

        discrepancies: list[StockDiscrepancy] = []
        for variant_id in variant_ids:
            level = levels[variant_id]
            delta = deltas[variant_id]
            resulting = level.quantity + delta
            if resulting < 0:
                if not allow_negative:
                    raise InsufficientStock(
                        f"Only {level.quantity} unit(s) available for {variants[variant_id].display_name}.",
                        variant=variant_id,
                        available=level.quantity,
                        requested=abs(delta),
                    )
                discrepancies.append(
                    StockDiscrepancy(
                        organization=organization,
                        location=location,
                        variant_id=variant_id,
                        quantity_before=level.quantity,
                        quantity_requested=delta,
                        quantity_after=resulting,
                        source_type=source_type,
                        source_id=str(source_id),
                        reason=reason or "Stock went negative applying an offline operation.",
                    )
                )
            level.quantity = resulting

        movements = [
            InventoryMovement(
                organization=organization,
                location=location,
                variant_id=str(line.variant_id),
                quantity=line.quantity,
                movement_type=movement_type,
                unit_cost=line.unit_cost,
                source_type=source_type,
                source_id=str(source_id),
                created_by=user,
                occurred_at=occurred_at,
                note=line.note or reason,
            )
            for line in lines
        ]
        InventoryMovement.objects.bulk_create(movements)

        # bulk_update does not run auto_now, so the timestamp is set by hand.
        touched = list(levels.values())
        now = timezone.now()
        for level in touched:
            level.updated_at = now
        StockLevel.objects.bulk_update(touched, ["quantity", "updated_at"])
        if discrepancies:
            StockDiscrepancy.objects.bulk_create(discrepancies)

        return movements

    @staticmethod
    def _lock_levels(organization, location, variant_ids: list[str]) -> dict[str, StockLevel]:
        """Create missing balance rows, then lock the whole set in a stable order."""
        StockLevel.objects.bulk_create(
            [
                StockLevel(organization=organization, location=location, variant_id=variant_id)
                for variant_id in variant_ids
            ],
            ignore_conflicts=True,
        )
        locked = (
            StockLevel.objects.select_for_update()
            .filter(location=location, variant_id__in=variant_ids)
            .order_by("variant_id")
        )
        levels = {str(level.variant_id): level for level in locked}
        if len(levels) != len(variant_ids):  # pragma: no cover - defensive
            raise InvalidOperation("Could not lock stock rows for this operation.")
        return levels

    @staticmethod
    def available(*, location, variant) -> int:
        level = StockLevel.objects.filter(location=location, variant=variant).first()
        return level.quantity if level else 0

    @staticmethod
    def ledger_balance(*, location, variant) -> int:
        """Recompute a balance straight from the ledger - the auditable truth."""
        result = InventoryMovement.objects.filter(location=location, variant=variant).aggregate(
            total=Sum("quantity")
        )
        return result["total"] or 0

    @staticmethod
    @transaction.atomic
    def recalculate(*, organization, location=None) -> dict[str, int]:
        """Rebuild StockLevel from the ledger. Also serves as a drift detector."""
        movements = InventoryMovement.objects.filter(organization=organization)
        if location is not None:
            movements = movements.filter(location=location)

        totals = {
            (str(row["location"]), str(row["variant"])): row["total"]
            for row in movements.values("location", "variant").annotate(total=Sum("quantity"))
        }

        levels = StockLevel.objects.filter(organization=organization)
        if location is not None:
            levels = levels.filter(location=location)
        levels = list(levels.select_for_update())

        corrected = 0
        now = timezone.now()
        for level in levels:
            expected = totals.pop((str(level.location_id), str(level.variant_id)), 0)
            if level.quantity != expected:
                level.quantity = expected
                level.updated_at = now
                corrected += 1
        StockLevel.objects.bulk_update(levels, ["quantity", "updated_at"])

        created = [
            StockLevel(
                organization=organization, location_id=loc_id, variant_id=variant_id, quantity=total
            )
            for (loc_id, variant_id), total in totals.items()
        ]
        StockLevel.objects.bulk_create(created)

        return {"levels_checked": len(levels), "levels_corrected": corrected, "levels_created": len(created)}

    @staticmethod
    def update_average_cost(*, variant: ProductVariant, incoming_quantity: int, unit_cost: Decimal) -> None:
        """Moving weighted average (decision D3).

        Call this *after* the PURCHASE movement has been recorded: it derives
        the pre-receipt balance by subtracting the incoming units from the
        current one, so it cannot double-count them. Cost is a property of the
        product, not of a location, so the balance is taken across all of them.
        """
        if incoming_quantity <= 0:
            return

        quantity_after = (
            StockLevel.objects.filter(variant=variant).aggregate(total=Sum("quantity"))["total"] or 0
        )
        previous_quantity = max(quantity_after - incoming_quantity, 0)

        previous_value = Decimal(previous_quantity) * variant.average_cost
        incoming_value = Decimal(incoming_quantity) * Decimal(unit_cost)
        total_quantity = previous_quantity + incoming_quantity

        variant.average_cost = (previous_value + incoming_value) / Decimal(total_quantity)
        variant.last_purchase_cost = Decimal(unit_cost)
        variant.save(update_fields=["average_cost", "last_purchase_cost", "updated_at"])


@transaction.atomic
def record_initial_stock(*, organization, location, lines, user=None, note="Initial stock"):
    movements = InventoryService.apply_movements(
        organization=organization,
        location=location,
        lines=lines,
        movement_type=MovementType.INITIAL_STOCK,
        user=user,
        source_type="initial",
        reason=note,
    )

    # The opening balance is the only cost a product has until its first
    # purchase is received, so a unit cost given here has to reach the variant
    # — otherwise it would sit on the movement row and the catalogue would keep
    # reporting no cost at all (and every margin with it). Adjustments
    # deliberately do NOT do this: losing three units does not restate what the
    # remaining ones cost.
    costed = {
        line.variant_id: line for line in lines if line.unit_cost is not None and line.quantity > 0
    }
    if costed:
        variants = ProductVariant.objects.filter(pk__in=costed).select_for_update()
        for variant in variants:
            line = costed[str(variant.pk)]
            InventoryService.update_average_cost(
                variant=variant,
                incoming_quantity=line.quantity,
                unit_cost=line.unit_cost,
            )

    return movements


def record_adjustment(*, organization, location, lines, user=None, reason="", allow_negative=False):
    return InventoryService.apply_movements(
        organization=organization,
        location=location,
        lines=lines,
        movement_type=MovementType.ADJUSTMENT,
        user=user,
        source_type="adjustment",
        reason=reason,
        allow_negative=allow_negative,
    )


def zero_out_stock(*, organization, variant, user=None, reason="Product deactivated"):
    """Bring every location's balance for this variant down to zero.

    Called when a product/variant is deactivated: it stops being sellable, so
    it should also stop counting as available stock. The drop is recorded as
    an ADJUSTMENT movement, keeping the ledger the source of truth instead of
    silently zeroing StockLevel.
    """
    levels = StockLevel.objects.filter(organization=organization, variant=variant).exclude(quantity=0)
    for level in levels:
        InventoryService.apply_movements(
            organization=organization,
            location=level.location,
            lines=[MovementLine(variant_id=str(variant.pk), quantity=-level.quantity)],
            movement_type=MovementType.ADJUSTMENT,
            user=user,
            source_type="deactivation",
            source_id=str(variant.pk),
            reason=reason,
        )
