"""Send Web Push alerts when a sale is recorded."""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from apps.accounts.models import Membership
from apps.core import capabilities as caps
from apps.sales.labels import seller_label

from .models import WebPushSubscription

logger = logging.getLogger(__name__)


def can_receive_sale_push(membership: Membership) -> bool:
    return membership.status == Membership.Status.ACTIVE and (
        membership.role == Membership.Role.OWNER
        or membership.has_capability(caps.ORGANIZATION_MANAGE)
    )


def recipient_user_ids(*, organization) -> list:
    memberships = Membership.objects.filter(
        organization=organization,
        status=Membership.Status.ACTIVE,
    ).select_related("user")
    return [m.user_id for m in memberships if can_receive_sale_push(m)]


def _format_money(amount: Decimal | str, currency: str = "COP") -> str:
    try:
        value = Decimal(str(amount))
    except Exception:
        return str(amount)
    if currency.upper() == "COP":
        whole = int(value.quantize(Decimal("1")))
        return f"${whole:,}".replace(",", ".")
    return f"{currency} {value}"


def notify_sale(*, organization, sale) -> None:
    """Push to OWNER / organization.manage subscribers. Never raises to callers."""
    public = getattr(settings, "VAPID_PUBLIC_KEY", "") or ""
    private = getattr(settings, "VAPID_PRIVATE_KEY", "") or ""
    subject = getattr(settings, "VAPID_SUBJECT", "mailto:admin@example.com")
    if not public or not private:
        logger.debug("VAPID keys not configured; skipping sale push")
        return

    user_ids = recipient_user_ids(organization=organization)
    if not user_ids:
        return

    # on_commit runs outside the request tenant contextvar.
    subscriptions = list(
        WebPushSubscription.all_objects.filter(
            organization_id=organization.pk,
            user_id__in=user_ids,
        )
    )
    if not subscriptions:
        return

    currency = getattr(organization, "currency", None) or "COP"
    title = "Nueva venta"
    body = f"{seller_label(sale)} registró una venta de {_format_money(sale.total, currency)}"
    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "url": "/?tab=sales-history",
        }
    )

    vapid_claims = {"sub": subject}
    vapid_private_key = private

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.exception("pywebpush is not installed")
        return

    stale: list[str] = []
    sent = 0
    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=vapid_private_key,
                vapid_claims=vapid_claims,
            )
            sent += 1
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            body = ""
            try:
                body = (exc.response.text or "")[:300] if exc.response is not None else ""
            except Exception:
                body = ""
            if status_code in (404, 410):
                stale.append(str(sub.pk))
            else:
                logger.warning(
                    "Web push failed for %s: %s body=%s",
                    sub.pk,
                    exc,
                    body,
                )
        except Exception:
            logger.exception("Unexpected web push error for %s", sub.pk)

    if stale:
        WebPushSubscription.all_objects.filter(pk__in=stale).delete()

    logger.info(
        "Sale push for %s: sent=%s failed_or_stale=%s total_subs=%s",
        getattr(sale, "number", sale.pk),
        sent,
        len(subscriptions) - sent,
        len(subscriptions),
    )


def schedule_sale_notification(*, organization, sale) -> None:
    """Fire after the sale transaction commits so a push failure never rolls back money."""
    org_id = organization.pk
    sale_id = sale.pk

    def _send():
        from apps.organizations.models import Organization
        from apps.sales.models import Sale

        # Organization is not tenant-scoped (it IS the tenant), so `.objects` is fine.
        org = Organization.objects.filter(pk=org_id).first()
        completed = (
            Sale.all_objects.select_related("seller", "organization")
            .filter(pk=sale_id)
            .first()
        )
        if org is None or completed is None:
            return
        try:
            notify_sale(organization=org, sale=completed)
        except Exception:
            # Never fail the HTTP response because of push delivery.
            logger.exception("Sale push notification failed for sale %s", sale_id)

    transaction.on_commit(_send)
