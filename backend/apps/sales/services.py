"""SaleService and RefundService.

Everything here runs inside a single database transaction. The invariant the
whole module exists to protect: **a completed sale and the stock it consumed
are written together, or neither is written.**

Concurrency is delegated to InventoryService, which locks the affected
StockLevel rows in a deterministic order. Refunds additionally lock their sale
row, so two cashiers refunding the same receipt at once cannot together return
more units than were sold.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.cash.models import CashMovementType, CashSession
from apps.cash.services import CashService
from apps.core.audit import record_audit
from apps.core.enums import PaymentMethod
from apps.core.exceptions import DomainError, InvalidOperation
from apps.core.models import DocumentSequence
from apps.core.money import money, split_tax_from_gross
from apps.core.sequences import next_document_number
from apps.inventory.models import MovementType
from apps.inventory.services import InventoryService, MovementLine

from .models import Payment, Refund, RefundItem, Sale, SaleItem


class PriceMismatch(DomainError):
    default_message = "The total calculated by the server differs from the one sent by the client."
    code = "price_mismatch"
    status_code = 409


class PaymentMismatch(DomainError):
    default_message = "Payments do not cover the total of the sale."
    code = "payment_mismatch"
    status_code = 400


class PriceOverrideNotAllowed(DomainError):
    default_message = "Your role cannot sell at a price other than the catalogue's."
    code = "price_override_not_allowed"
    status_code = 403


@dataclass
class _Line:
    variant: object
    quantity: int
    unit_price: Decimal
    discount_amount: Decimal
    tax_rate: Decimal
    gross: Decimal
    taxable_base: Decimal
    tax_amount: Decimal


def _build_lines(raw_lines, *, tax_rate: Decimal, allow_price_override: bool = True) -> list[_Line]:
    """Recompute every line on the server. Client-sent totals are never trusted.

    `tax_rate` is the selling business's own rate (zero when it does not charge
    IVA at all): tax is a per-business setting, never a per-product one.
    """
    lines: list[_Line] = []
    for raw in raw_lines:
        variant = raw["variant"]
        quantity = int(raw["quantity"])
        if quantity < 1:
            raise InvalidOperation("Quantity must be at least 1.", variant=str(variant.pk))

        # A price may be overridden at the till (haggling is normal in retail),
        # but the default is always the shelf price from the catalogue, and only
        # a role holding sales.override_price may replace it.
        unit_price = money(raw.get("unit_price") if raw.get("unit_price") is not None else variant.price)
        if not allow_price_override and unit_price != money(variant.price):
            raise PriceOverrideNotAllowed(
                variant=str(variant.pk),
                catalogue_price=str(money(variant.price)),
                requested_price=str(unit_price),
            )
        discount = money(raw.get("discount_amount") or 0)
        gross = money(unit_price * quantity - discount)
        if gross < 0:
            raise InvalidOperation(
                "A line discount cannot exceed the line total.", variant=str(variant.pk)
            )

        base, tax = split_tax_from_gross(gross, tax_rate)
        lines.append(
            _Line(
                variant=variant,
                quantity=quantity,
                unit_price=unit_price,
                discount_amount=discount,
                tax_rate=tax_rate,
                gross=gross,
                taxable_base=base,
                tax_amount=tax,
            )
        )
    return lines


def _resolve_cash_session(*, organization, cash_register, needs_cash: bool) -> CashSession | None:
    """Find the open shift for a register, if the caller supplied one.

    If the caller took cash but did not say which register, and the
    organization has an open session somewhere, refuse instead of silently
    completing the sale outside every drawer - that is exactly how a real
    cash sale goes missing from the day's arqueo. A store with no open
    session anywhere (cash control genuinely unused today) is unaffected.
    """
    if cash_register is None:
        if needs_cash and CashSession.objects.filter(
            register__organization=organization, status=CashSession.Status.OPEN
        ).exists():
            raise InvalidOperation(
                "This organization has an open cash session. Specify cash_register on the "
                "cash payment so it is tracked in the correct drawer.",
            )
        return None

    session = CashSession.objects.filter(
        register=cash_register, status=CashSession.Status.OPEN
    ).first()
    if session is None and needs_cash:
        raise InvalidOperation(
            "This register has no open cash session. Open the register before taking cash.",
            register=str(cash_register.pk),
        )
    return session


class SaleService:
    @staticmethod
    @transaction.atomic
    def create_sale(
        *,
        organization,
        location,
        lines,
        payments,
        user=None,
        customer=None,
        cash_register=None,
        occurred_at=None,
        notes: str = "",
        source: str = Sale.Source.POS,
        device_id: str = "",
        expected_total=None,
        allow_negative_stock: bool = False,
        sale_id=None,
        allow_price_override: bool = True,
    ) -> Sale:
        if not lines:
            raise InvalidOperation("A sale needs at least one item.")
        if customer is None:
            raise InvalidOperation("A sale requires a customer.")

        occurred_at = occurred_at or timezone.now()
        built = _build_lines(
            lines,
            tax_rate=organization.effective_tax_rate,
            allow_price_override=allow_price_override,
        )

        subtotal = money(sum(line.unit_price * line.quantity for line in built))
        discount_total = money(sum(line.discount_amount for line in built))
        total = money(sum(line.gross for line in built))
        tax_total = money(sum(line.tax_amount for line in built))

        if expected_total is not None and money(expected_total) != total:
            # Typically an offline terminal working from a stale price list.
            raise PriceMismatch(
                f"Server total is {total}, client sent {money(expected_total)}.",
                server_total=str(total),
                client_total=str(money(expected_total)),
            )

        paid_total = money(sum(Decimal(p["amount"]) for p in payments))
        cash_paid = money(
            sum(Decimal(p["amount"]) for p in payments if p["method"] == PaymentMethod.CASH)
        )
        if paid_total < total:
            raise PaymentMismatch(
                f"Payments total {paid_total} but the sale is {total}.",
                total=str(total),
                paid=str(paid_total),
            )
        change = money(paid_total - total)
        if change > 0 and cash_paid < change:
            # Change can only be given from cash actually received.
            raise PaymentMismatch(
                "Overpayment is only allowed when paying cash.",
                total=str(total),
                paid=str(paid_total),
            )

        session = _resolve_cash_session(
            organization=organization, cash_register=cash_register, needs_cash=cash_paid > 0
        )

        # The id is minted here rather than by the database so the stock
        # movements can reference the sale before the row exists. An offline
        # terminal passes the id it already printed on the customer's receipt.
        sale_id = sale_id or uuid.uuid4()

        # Stock first: if it fails, nothing else is written - one transaction.
        stock_lines = [
            MovementLine(
                variant_id=str(line.variant.pk),
                quantity=-line.quantity,
                unit_cost=line.variant.average_cost,
            )
            for line in built
            if line.variant.product.track_inventory
        ]
        if stock_lines:
            InventoryService.apply_movements(
                organization=organization,
                location=location,
                lines=stock_lines,
                movement_type=MovementType.SALE,
                user=user,
                source_type="sale",
                source_id=str(sale_id),
                occurred_at=occurred_at,
                allow_negative=allow_negative_stock,
            )

        number = next_document_number(
            organization=organization,
            location=location,
            document_type=DocumentSequence.DocumentType.SALE,
        )

        sale = Sale.objects.create(
            id=sale_id,
            organization=organization,
            number=number,
            location=location,
            status=Sale.Status.COMPLETED,
            customer=customer,
            seller=user,
            cash_session=session,
            subtotal=subtotal,
            discount_total=discount_total,
            tax_total=tax_total,
            total=total,
            paid_total=paid_total,
            change_amount=change,
            source=source,
            device_id=device_id,
            occurred_at=occurred_at,
            notes=notes,
        )

        SaleItem.objects.bulk_create(
            [
                SaleItem(
                    organization=organization,
                    sale=sale,
                    variant=line.variant,
                    description=line.variant.display_name,
                    sku=line.variant.sku,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    discount_amount=line.discount_amount,
                    line_total=line.gross,
                    tax_rate=line.tax_rate,
                    taxable_base=line.taxable_base,
                    tax_amount=line.tax_amount,
                    unit_cost=line.variant.average_cost,
                )
                for line in built
            ]
        )

        Payment.objects.bulk_create(
            [
                Payment(
                    organization=organization,
                    sale=sale,
                    method=payment["method"],
                    amount=money(payment["amount"]),
                    reference=payment.get("reference", ""),
                )
                for payment in payments
            ]
        )

        if session is not None and cash_paid > 0:
            CashService.record_movement(
                session=session,
                movement_type=CashMovementType.SALE,
                amount=cash_paid - change,
                user=user,
                source_type="sale",
                source_id=str(sale.pk),
                note=f"Venta {sale.number}",
            )

        record_audit(
            organization=organization,
            action="sale.created",
            actor=user,
            obj=sale,
            metadata={
                "number": sale.number,
                "total": str(total),
                "items": len(built),
                "source": source,
            },
        )
        from apps.notifications.services import schedule_sale_notification

        schedule_sale_notification(organization=organization, sale=sale)
        return sale

    @staticmethod
    @transaction.atomic
    def cancel_sale(*, sale: Sale, user=None, reason: str = "") -> Sale:
        """Void a whole sale: stock goes back, cash comes out.

        Only possible while nothing has been refunded. Once a partial refund
        exists the sale has a history, and cancelling would erase it - refund
        the remainder instead.
        """
        locked = Sale.objects.select_for_update().get(pk=sale.pk)
        if locked.status != Sale.Status.COMPLETED:
            raise InvalidOperation(
                f"Only a completed sale can be cancelled (this one is {locked.status}).",
                sale=str(locked.pk),
            )
        if locked.refunds.exists():
            raise InvalidOperation(
                "This sale already has refunds. Refund the remaining units instead of cancelling.",
                sale=str(locked.pk),
            )

        items = list(locked.items.select_related("variant", "variant__product").all())
        restock = [
            MovementLine(
                variant_id=str(item.variant_id), quantity=item.quantity, unit_cost=item.unit_cost
            )
            for item in items
            if item.variant.product.track_inventory
        ]
        if restock:
            InventoryService.apply_movements(
                organization=locked.organization,
                location=locked.location,
                lines=restock,
                movement_type=MovementType.RETURN,
                user=user,
                source_type="sale.cancelled",
                source_id=str(locked.pk),
                reason=reason or "Venta anulada",
            )

        cash_in = money(
            locked.payments.filter(method=PaymentMethod.CASH).aggregate(total=Sum("amount"))["total"]
            or 0
        )
        cash_effect = money(cash_in - locked.change_amount)
        if locked.cash_session_id and cash_effect > 0 and locked.cash_session.is_open:
            CashService.record_movement(
                session=locked.cash_session,
                movement_type=CashMovementType.REFUND,
                amount=-cash_effect,
                user=user,
                source_type="sale.cancelled",
                source_id=str(locked.pk),
                note=f"Anulación {locked.number}",
            )

        locked.status = Sale.Status.CANCELLED
        locked.cancelled_at = timezone.now()
        locked.cancelled_by = user
        locked.cancellation_reason = reason
        locked.save(
            update_fields=["status", "cancelled_at", "cancelled_by", "cancellation_reason", "updated_at"]
        )

        record_audit(
            organization=locked.organization,
            action="sale.cancelled",
            actor=user,
            obj=locked,
            metadata={"number": locked.number, "reason": reason, "total": str(locked.total)},
        )
        return locked


class RefundService:
    @staticmethod
    @transaction.atomic
    def create_refund(
        *,
        sale: Sale,
        lines,
        user=None,
        method: str = PaymentMethod.CASH,
        restock: bool = True,
        reason: str = "",
        cash_register=None,
        occurred_at=None,
    ) -> Refund:
        """Return units from one sale. Never more than were sold and not yet returned."""
        if not lines:
            raise InvalidOperation("A refund needs at least one line.")

        # Locking the sale serialises concurrent refunds against the same receipt.
        locked_sale = Sale.objects.select_for_update().get(pk=sale.pk)
        if locked_sale.status not in (Sale.Status.COMPLETED, Sale.Status.PARTIALLY_REFUNDED):
            raise InvalidOperation(
                f"A sale in status {locked_sale.status} cannot be refunded.",
                sale=str(locked_sale.pk),
            )

        occurred_at = occurred_at or timezone.now()
        item_ids = [str(line["sale_item"].pk) for line in lines]
        if len(set(item_ids)) != len(item_ids):
            raise InvalidOperation("Each sale item may appear only once per refund.")

        items = {
            str(item.pk): item
            for item in SaleItem.objects.select_for_update()
            .filter(sale=locked_sale, pk__in=item_ids)
            .select_related("variant", "variant__product")
            .order_by("pk")
        }
        if len(items) != len(item_ids):
            raise InvalidOperation("Those items do not belong to this sale.", sale=str(locked_sale.pk))

        already_refunded = {
            str(row["sale_item"]): row["total"]
            for row in RefundItem.objects.filter(sale_item__in=items.values())
            .values("sale_item")
            .annotate(total=Sum("amount"))
        }

        refund_lines = []
        total = Decimal("0.00")
        for line in lines:
            item = items[str(line["sale_item"].pk)]
            quantity = int(line["quantity"])
            if quantity < 1:
                raise InvalidOperation("Refund quantity must be at least 1.", item=str(item.pk))
            if quantity > item.refundable_quantity:
                raise InvalidOperation(
                    f"Cannot refund {quantity} of {item.description}: "
                    f"{item.refundable_quantity} unit(s) remain refundable.",
                    item=str(item.pk),
                    sold=item.quantity,
                    already_refunded=item.refunded_quantity,
                    requested=quantity,
                )

            if quantity == item.refundable_quantity:
                # Last units: return the exact remainder so rounding never
                # leaves a few pesos permanently unrefundable.
                amount = money(item.line_total - (already_refunded.get(str(item.pk)) or Decimal("0.00")))
            else:
                amount = money(item.line_total * Decimal(quantity) / Decimal(item.quantity))

            total += amount
            refund_lines.append((item, quantity, amount))

        total = money(total)
        session = _resolve_cash_session(
            organization=locked_sale.organization,
            cash_register=cash_register,
            needs_cash=method == PaymentMethod.CASH,
        )

        refund = Refund.objects.create(
            organization=locked_sale.organization,
            sale=locked_sale,
            location=locked_sale.location,
            cash_session=session,
            total=total,
            method=method,
            restock=restock,
            reason=reason,
            occurred_at=occurred_at,
            created_by=user,
            number=next_document_number(
                organization=locked_sale.organization,
                location=locked_sale.location,
                document_type=DocumentSequence.DocumentType.REFUND,
            ),
        )

        RefundItem.objects.bulk_create(
            [
                RefundItem(
                    organization=locked_sale.organization,
                    refund=refund,
                    sale_item=item,
                    quantity=quantity,
                    amount=amount,
                )
                for item, quantity, amount in refund_lines
            ]
        )

        if restock:
            stock_lines = [
                MovementLine(variant_id=str(item.variant_id), quantity=quantity, unit_cost=item.unit_cost)
                for item, quantity, _ in refund_lines
                if item.variant.product.track_inventory
            ]
            if stock_lines:
                InventoryService.apply_movements(
                    organization=locked_sale.organization,
                    location=locked_sale.location,
                    lines=stock_lines,
                    movement_type=MovementType.RETURN,
                    user=user,
                    source_type="refund",
                    source_id=str(refund.pk),
                    occurred_at=occurred_at,
                    reason=reason,
                )

        if session is not None and method == PaymentMethod.CASH:
            CashService.record_movement(
                session=session,
                movement_type=CashMovementType.REFUND,
                amount=-total,
                user=user,
                source_type="refund",
                source_id=str(refund.pk),
                note=f"Devolución {refund.number}",
            )

        for item, quantity, _ in refund_lines:
            item.refunded_quantity += quantity
            item.save(update_fields=["refunded_quantity", "updated_at"])

        locked_sale.refunded_total = money(locked_sale.refunded_total + total)
        fully_refunded = not any(
            item.refundable_quantity for item in locked_sale.items.all()
        )
        locked_sale.status = Sale.Status.REFUNDED if fully_refunded else Sale.Status.PARTIALLY_REFUNDED
        locked_sale.save(update_fields=["refunded_total", "status", "updated_at"])

        record_audit(
            organization=locked_sale.organization,
            action="sale.refunded",
            actor=user,
            obj=refund,
            metadata={
                "sale": locked_sale.number,
                "refund": refund.number,
                "total": str(total),
                "restock": restock,
                "reason": reason,
            },
        )
        return refund
