from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Plan, Subscription, SubscriptionPayment

TRIAL_DAYS = 14


def start_trial_subscription(*, organization, plan_code: str = Plan.Code.BASIC) -> Subscription:
    plan = Plan.objects.filter(code=plan_code, is_active=True).first() or Plan.objects.order_by(
        "sort_order"
    ).first()
    if plan is None:
        raise RuntimeError("No subscription plans are configured. Run `manage.py migrate`.")

    now = timezone.now()
    return Subscription.objects.create(
        organization=organization,
        plan=plan,
        status=Subscription.Status.TRIAL,
        trial_ends_at=now + timedelta(days=TRIAL_DAYS),
        current_period_start=now,
        current_period_end=now + timedelta(days=TRIAL_DAYS),
    )


def start_active_subscription(
    *,
    organization,
    plan: Plan | None = None,
    plan_code: str = Plan.Code.BASIC,
    months: int = 1,
    trial_days: int | None = None,
) -> Subscription:
    """Used by platform operators when provisioning a business."""
    if plan is None:
        plan = Plan.objects.filter(code=plan_code, is_active=True).first() or Plan.objects.order_by(
            "sort_order"
        ).first()
    if plan is None:
        raise RuntimeError("No subscription plans are configured. Run `manage.py migrate`.")

    now = timezone.now()
    if trial_days is not None and trial_days > 0:
        end = now + timedelta(days=trial_days)
        return Subscription.objects.create(
            organization=organization,
            plan=plan,
            status=Subscription.Status.TRIAL,
            trial_ends_at=end,
            current_period_start=now,
            current_period_end=end,
        )

    months = max(1, months)
    end = now + timedelta(days=30 * months)
    return Subscription.objects.create(
        organization=organization,
        plan=plan,
        status=Subscription.Status.ACTIVE,
        trial_ends_at=None,
        current_period_start=now,
        current_period_end=end,
    )


def expire_if_needed(subscription: Subscription) -> Subscription:
    """Mark EXPIRED when the trial/period has elapsed. Idempotent."""
    if subscription.status in (
        Subscription.Status.EXPIRED,
        Subscription.Status.CANCELLED,
    ):
        return subscription

    now = timezone.now()
    should_expire = False
    if subscription.status == Subscription.Status.TRIAL:
        should_expire = (
            subscription.trial_ends_at is not None and subscription.trial_ends_at <= now
        )
    elif subscription.status in (
        Subscription.Status.ACTIVE,
        Subscription.Status.PAST_DUE,
    ):
        should_expire = (
            subscription.current_period_end is not None
            and subscription.current_period_end <= now
        )

    if should_expire:
        subscription.status = Subscription.Status.EXPIRED
        subscription.save(update_fields=["status", "updated_at"])
    return subscription


@transaction.atomic
def record_payment(
    *,
    organization,
    amount: Decimal,
    period_start,
    period_end,
    paid_at=None,
    currency: str = "COP",
    method: str = SubscriptionPayment.Method.TRANSFER,
    reference: str = "",
    notes: str = "",
    recorded_by=None,
    plan: Plan | None = None,
) -> SubscriptionPayment:
    """Record a manual payment and extend/activate the subscription."""
    subscription = Subscription.all_objects.select_for_update().filter(organization=organization).first()
    if subscription is None:
        raise RuntimeError("Organization has no subscription.")

    if plan is not None:
        subscription.plan = plan

    paid_at = paid_at or timezone.now()
    payment = SubscriptionPayment.objects.create(
        organization=organization,
        subscription=subscription,
        amount=amount,
        currency=currency,
        paid_at=paid_at,
        period_start=period_start,
        period_end=period_end,
        method=method,
        reference=reference,
        notes=notes,
        recorded_by=recorded_by,
    )

    subscription.status = Subscription.Status.ACTIVE
    subscription.current_period_start = period_start
    subscription.current_period_end = period_end
    subscription.cancelled_at = None
    subscription.trial_ends_at = None
    subscription.save(
        update_fields=[
            "plan",
            "status",
            "current_period_start",
            "current_period_end",
            "cancelled_at",
            "trial_ends_at",
            "updated_at",
        ]
    )
    return payment
