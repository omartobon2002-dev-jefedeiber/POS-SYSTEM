"""CashService - opening, movements, arqueo and cashier handoff."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.accounts.models import Membership
from apps.core import capabilities as caps
from apps.core.audit import record_audit
from apps.core.exceptions import InvalidOperation
from apps.core.money import money

from .models import CashMovement, CashMovementType, CashRegister, CashSession


class RegisterAlreadyUsedToday(InvalidOperation):
    """A register may only be opened once per calendar day (except via transfer)."""

    default_message = (
        "This register already had a shift today. Close it or hand it off to another cashier."
    )
    code = "register_already_used_today"
    status_code = 409


def local_day_bounds(organization) -> tuple[datetime, datetime]:
    """Start/end of the organization's local calendar day as aware datetimes."""
    tz_name = getattr(organization, "timezone", None) or "America/Bogota"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Bogota")
    now_local = timezone.now().astimezone(tz)
    start = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


class CashService:
    @staticmethod
    @transaction.atomic
    def open_session(
        *,
        organization,
        register: CashRegister,
        user,
        opening_amount=0,
        notes="",
        allow_same_day_reopen: bool = False,
        previous_session: CashSession | None = None,
    ) -> CashSession:
        if not register.is_active:
            raise InvalidOperation("This register is inactive.", register=str(register.pk))

        # The partial unique index is the real guard; this check turns a
        # database error into a readable one.
        if CashSession.objects.filter(register=register, status=CashSession.Status.OPEN).exists():
            raise InvalidOperation(
                "This register already has an open session. Close it before opening another.",
                register=str(register.pk),
            )

        if not allow_same_day_reopen:
            day_start, day_end = local_day_bounds(organization)
            if CashSession.objects.filter(
                register=register,
                opened_at__gte=day_start,
                opened_at__lt=day_end,
            ).exists():
                raise RegisterAlreadyUsedToday(register=str(register.pk))

        opened_at = timezone.now()
        session = CashSession.objects.create(
            organization=organization,
            register=register,
            opened_by=user,
            opened_at=opened_at,
            opening_amount=money(opening_amount),
            notes=notes,
            previous_session=previous_session,
        )
        if session.opening_amount:
            CashService.record_movement(
                session=session,
                movement_type=CashMovementType.OPENING,
                amount=session.opening_amount,
                user=user,
                note="Base inicial",
            )

        record_audit(
            organization=organization,
            action="cash.opened",
            actor=user,
            obj=session,
            metadata={"register": register.code, "opening_amount": str(session.opening_amount)},
        )
        return session

    @staticmethod
    def record_movement(
        *,
        session: CashSession,
        movement_type: str,
        amount,
        user=None,
        source_type: str = "",
        source_id: str = "",
        note: str = "",
    ) -> CashMovement:
        if not session.is_open:
            raise InvalidOperation("The cash session is closed.", session=str(session.pk))
        amount = money(amount)
        if amount == 0:
            raise InvalidOperation("A cash movement cannot be zero.")

        return CashMovement.objects.create(
            organization=session.organization,
            session=session,
            movement_type=movement_type,
            amount=amount,
            source_type=source_type,
            source_id=str(source_id),
            created_by=user,
            note=note,
        )

    @staticmethod
    def expected_amount(session: CashSession) -> Decimal:
        """What the drawer should hold: the sum of its movements."""
        total = session.movements.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        return money(total)

    @staticmethod
    @transaction.atomic
    def close_session(
        *,
        session: CashSession,
        counted_amount,
        user,
        notes: str = "",
        close_reason: str = CashSession.CloseReason.NORMAL,
    ) -> CashSession:
        """Arqueo: compare what was counted against what the movements imply."""
        locked = CashSession.objects.select_for_update().get(pk=session.pk)
        if not locked.is_open:
            raise InvalidOperation("This session is already closed.", session=str(locked.pk))

        expected = CashService.expected_amount(locked)
        counted = money(counted_amount)

        locked.status = CashSession.Status.CLOSED
        locked.closed_by = user
        locked.closed_at = timezone.now()
        locked.expected_amount = expected
        locked.counted_amount = counted
        # Positive means surplus in the drawer, negative means shortfall.
        locked.difference = money(counted - expected)
        locked.close_reason = close_reason or CashSession.CloseReason.NORMAL
        if notes:
            locked.notes = f"{locked.notes}\n{notes}".strip()
        locked.save(
            update_fields=[
                "status",
                "closed_by",
                "closed_at",
                "expected_amount",
                "counted_amount",
                "difference",
                "close_reason",
                "notes",
                "updated_at",
            ]
        )

        record_audit(
            organization=locked.organization,
            action="cash.closed",
            actor=user,
            obj=locked,
            metadata={
                "expected": str(expected),
                "counted": str(counted),
                "difference": str(locked.difference),
                "close_reason": locked.close_reason,
            },
        )
        return locked

    @staticmethod
    @transaction.atomic
    def transfer_session(
        *,
        session: CashSession,
        counted_amount,
        from_user,
        to_user,
        notes: str = "",
    ) -> tuple[CashSession, CashSession]:
        """Hand the drawer to another cashier: arqueo + open a same-day successor."""
        locked = CashSession.objects.select_for_update().get(pk=session.pk)
        if not locked.is_open:
            raise InvalidOperation("This session is already closed.", session=str(locked.pk))

        membership = (
            Membership.objects.filter(
                organization_id=locked.organization_id,
                user_id=to_user.pk,
                status=Membership.Status.ACTIVE,
            )
            .select_related("user")
            .first()
        )
        if membership is None:
            raise InvalidOperation(
                "The destination user is not an active member of this organization.",
                to_user=str(to_user.pk),
            )
        if not membership.has_capability(caps.CASH_OPEN):
            raise InvalidOperation(
                "The destination user cannot open a cash session.",
                to_user=str(to_user.pk),
            )
        if to_user.pk == locked.opened_by_id:
            raise InvalidOperation(
                "Choose a different cashier to hand the register to.",
                to_user=str(to_user.pk),
            )

        counted = money(counted_amount)
        handoff_note = notes or f"Traspaso a {membership.full_name or membership.username}"
        closed = CashService.close_session(
            session=locked,
            counted_amount=counted,
            user=from_user,
            notes=handoff_note,
            close_reason=CashSession.CloseReason.TRANSFER,
        )

        opened = CashService.open_session(
            organization=closed.organization,
            register=closed.register,
            user=to_user,
            opening_amount=counted,
            notes=f"Continuación tras traspaso desde sesión {closed.pk}",
            allow_same_day_reopen=True,
            previous_session=closed,
        )

        closed.superseded_by = opened
        closed.save(update_fields=["superseded_by", "updated_at"])

        record_audit(
            organization=closed.organization,
            action="cash.transferred",
            actor=from_user,
            obj=opened,
            metadata={
                "from_session": str(closed.pk),
                "to_session": str(opened.pk),
                "to_user": str(to_user.pk),
                "counted_amount": str(counted),
            },
        )
        return closed, opened

    @staticmethod
    def session_summary(session: CashSession) -> dict:
        """Everything the closing screen needs, including non-cash totals."""
        from apps.sales.models import Payment, Sale

        by_type = {
            row["movement_type"]: money(row["total"])
            for row in session.movements.values("movement_type").annotate(total=Sum("amount"))
        }
        by_method = {
            row["method"]: money(row["total"])
            for row in Payment.objects.filter(sale__cash_session=session)
            .values("method")
            .annotate(total=Sum("amount"))
        }
        sales = Sale.objects.filter(cash_session=session, status__in=Sale.SETTLED_STATUSES)

        sales_cash = by_type.get(CashMovementType.SALE, money(0))
        refunds_cash = by_type.get(CashMovementType.REFUND, money(0))
        deposits = by_type.get(CashMovementType.DEPOSIT, money(0))
        withdrawals = by_type.get(CashMovementType.WITHDRAWAL, money(0))

        return {
            "expected_amount": CashService.expected_amount(session),
            "opening_amount": session.opening_amount,
            "movements_by_type": by_type,
            "payments_by_method": by_method,
            "sales_count": sales.count(),
            "sales_total": money(sales.aggregate(total=Sum("total"))["total"] or 0),
            # Convenience totals for the arqueo UI (withdrawals/refunds as positive magnitudes).
            "total_sales_cash": sales_cash,
            "total_refunds_cash": abs(refunds_cash),
            "total_deposits": deposits,
            "total_withdrawals": abs(withdrawals),
        }
