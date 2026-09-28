"""Recording an expense, and the drawer movement that must accompany it."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.cash.models import CashMovement, CashMovementType, CashSession
from apps.cash.services import CashService, ensure_not_stale
from apps.core.audit import record_audit
from apps.core.enums import PaymentMethod
from apps.core.exceptions import InvalidOperation
from apps.core.money import money

from .models import Expense

SOURCE_TYPE = "expense"


def open_session_for(*, location, provided=None) -> CashSession | None:
    """Which drawer this cash expense comes out of.

    Explicit beats inferred when money is involved: with two tills open at the
    same location the caller has to say which one, because guessing would make
    one of the two arqueos wrong.
    """
    if provided is not None:
        if not provided.is_open:
            raise InvalidOperation("That cash session is already closed.", session=str(provided.pk))
        ensure_not_stale(provided)
        return provided

    sessions = list(
        CashSession.objects.filter(register__location=location, status=CashSession.Status.OPEN)
    )
    if len(sessions) > 1:
        raise InvalidOperation(
            "There is more than one open register at this location: say which drawer the money "
            "comes out of.",
            open_sessions=[str(session.pk) for session in sessions],
        )
    session = sessions[0] if sessions else None
    # Money out of yesterday's drawer would falsify its closed-day count.
    ensure_not_stale(session)
    return session


@transaction.atomic
def record_expense(
    *,
    organization,
    category,
    location,
    amount,
    payment_method=PaymentMethod.CASH,
    occurred_at=None,
    cash_session=None,
    user=None,
    **fields,
) -> Expense:
    """Register money going out, and take it out of the drawer when it is cash.

    A cash expense with no open register is still recorded - the owner paid it
    from their own pocket or from the safe, and refusing it would only push the
    figure out of the system entirely.
    """
    amount = money(amount)
    if amount <= 0:
        raise InvalidOperation("An expense must be greater than zero.")

    expense = Expense.objects.create(
        organization=organization,
        category=category,
        location=location,
        supplier=fields.get("supplier"),
        description=fields.get("description", ""),
        amount=amount,
        payment_method=payment_method,
        occurred_at=occurred_at or timezone.now(),
        reference=fields.get("reference", ""),
        note=fields.get("note", ""),
        created_by=user,
    )

    if PaymentMethod.affects_drawer(payment_method):
        session = open_session_for(location=location, provided=cash_session)
        if session is not None:
            CashService.record_movement(
                session=session,
                movement_type=CashMovementType.WITHDRAWAL,
                # Negative: the drawer holds less after this.
                amount=-amount,
                user=user,
                source_type=SOURCE_TYPE,
                source_id=str(expense.pk),
                note=expense.description[:240],
            )
            expense.cash_session = session
            expense.save(update_fields=["cash_session", "updated_at"])

    record_audit(
        organization=organization,
        action="expense.recorded",
        actor=user,
        obj=expense,
        metadata={
            "category": category.name,
            "amount": str(expense.amount),
            "payment_method": payment_method,
            "from_drawer": expense.paid_from_drawer,
        },
    )
    return expense


@transaction.atomic
def delete_expense(*, expense: Expense, user=None) -> None:
    """Undo a mistyped expense, drawer movement included.

    Only while its shift is still open: once the arqueo is closed that number
    is part of a count somebody signed off on, and the correction is a cash
    adjustment, not a quiet deletion.
    """
    movement = CashMovement.objects.filter(
        source_type=SOURCE_TYPE, source_id=str(expense.pk)
    ).select_related("session").first()

    if movement is not None and not movement.session.is_open:
        raise InvalidOperation(
            "This expense was already counted in a closed arqueo. Correct it with a cash "
            "adjustment instead of deleting it.",
            session=str(movement.session_id),
        )

    record_audit(
        organization=expense.organization,
        action="expense.deleted",
        actor=user,
        obj=expense,
        metadata={"amount": str(expense.amount), "description": expense.description},
    )
    if movement is not None:
        movement.delete()
    expense.delete()
