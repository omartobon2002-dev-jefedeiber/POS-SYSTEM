"""SyncService - replay offline operations exactly once.

Deduplication contract
----------------------
`operation_id` is unique per tenant. A repeated push finds the existing row and
returns its stored result instead of executing anything, so a till that retries
a whole batch after a timeout changes nothing.

This is the same guarantee as the HTTP `Idempotency-Key`, but it does not use
the IdempotencyKey table: a sync push carries *many* operations and returns a
result per operation, not one replayed HTTP response. What sync needs on top -
the device, the raw payload, the failure reason, when the terminal actually
performed it - belongs on a record of its own.

Out-of-stock policy (decision D4)
---------------------------------
Replayed sales pass `allow_negative_stock=True`. The goods physically left the
shelf while the terminal was offline; refusing the record does not put them
back. Stock may go negative and a StockDiscrepancy is opened for a human.

Each operation is processed in its own transaction. One bad operation in a
batch must not roll back the twenty valid ones around it.
"""
from __future__ import annotations

import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core.exceptions import DomainError, InvalidOperation
from apps.organizations.selectors import default_location
from apps.sales.models import Sale
from apps.sales.serializers import RefundCreateSerializer, SaleCreateSerializer
from apps.sales.services import RefundService, SaleService

from .models import Device, SyncOperation


class SyncService:
    @staticmethod
    def push(
        *,
        organization,
        device: Device,
        operations: list[dict],
        user=None,
        allow_price_override: bool = True,
    ) -> list[dict]:
        """Process a batch. Returns one result per operation, in order."""
        results = [
            SyncService.process_one(
                organization=organization,
                device=device,
                operation=operation,
                user=user,
                allow_price_override=allow_price_override,
            )
            for operation in operations
        ]
        Device.objects.filter(pk=device.pk).update(
            last_sync_at=timezone.now(), last_seen_at=timezone.now()
        )
        return results

    @staticmethod
    def process_one(
        *, organization, device: Device, operation: dict, user=None, allow_price_override: bool = True
    ) -> dict:
        operation_id = str(operation["operation_id"])

        existing = SyncOperation.objects.filter(operation_id=operation_id).first()
        if existing is not None:
            return SyncService._as_result(existing, duplicate=True)

        handler = _HANDLERS.get(operation["operation_type"])
        if handler is None:  # pragma: no cover - the serializer already restricts this
            return {
                "operation_id": operation_id,
                "status": SyncOperation.Status.FAILED,
                "duplicate": False,
                "error_code": "unsupported_operation",
                "detail": f"Unsupported operation type {operation['operation_type']}.",
            }

        try:
            with transaction.atomic():
                record = SyncOperation.objects.create(
                    organization=organization,
                    operation_id=operation_id,
                    device=device,
                    operation_type=operation["operation_type"],
                    status=SyncOperation.Status.PROCESSED,
                    payload=operation.get("payload") or {},
                    occurred_at=operation.get("occurred_at"),
                    processed_by=user,
                )
                record.result = handler(
                    organization=organization,
                    device=device,
                    operation=operation,
                    user=user,
                    allow_price_override=allow_price_override,
                )
                record.save(update_fields=["result", "updated_at"])
        except IntegrityError:
            # Two pushes of the same operation raced. The winner's row is the
            # answer for both.
            duplicate = SyncOperation.objects.filter(operation_id=operation_id).first()
            if duplicate is not None:
                return SyncService._as_result(duplicate, duplicate=True)
            raise
        except (DomainError, ValidationError) as exc:
            # Recorded, not retried blindly: a rejected operation needs a human
            # or a corrected payload, and the terminal must stop resending it.
            failed = SyncService._record_failure(
                organization=organization,
                device=device,
                operation=operation,
                operation_id=operation_id,
                user=user,
                exc=exc,
            )
            return SyncService._as_result(failed, duplicate=False)

        return SyncService._as_result(record, duplicate=False)

    @staticmethod
    def _record_failure(*, organization, device, operation, operation_id, user, exc) -> SyncOperation:
        if isinstance(exc, DomainError):
            code, detail = exc.code, exc.message
        else:
            code, detail = "validation_error", str(exc.detail)

        return SyncOperation.objects.create(
            organization=organization,
            operation_id=operation_id,
            device=device,
            operation_type=operation["operation_type"],
            status=SyncOperation.Status.FAILED,
            payload=operation.get("payload") or {},
            occurred_at=operation.get("occurred_at"),
            processed_by=user,
            error_code=code,
            error_detail=detail,
        )

    @staticmethod
    def _as_result(record: SyncOperation, *, duplicate: bool) -> dict:
        return {
            "operation_id": str(record.operation_id),
            "operation_type": record.operation_type,
            "status": record.status,
            "duplicate": duplicate,
            "result": record.result,
            "error_code": record.error_code or None,
            "detail": record.error_detail or None,
        }


def _handle_sale_create(*, organization, device, operation, user, allow_price_override=True):
    payload = dict(operation.get("payload") or {})
    payload.setdefault("id", str(operation["operation_id"]))

    serializer = SaleCreateSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    location = data.get("location") or device.location or default_location()
    sale = SaleService.create_sale(
        organization=organization,
        location=location,
        lines=data["lines"],
        payments=data["payments"],
        user=user,
        customer=data["customer"],
        cash_register=data.get("cash_register") or device.cash_register,
        occurred_at=data.get("occurred_at") or operation.get("occurred_at"),
        notes=data.get("notes", ""),
        expected_total=data.get("expected_total"),
        sale_id=data.get("id"),
        source=Sale.Source.SYNC,
        device_id=device.identifier,
        # Decision D4: the sale already happened in the store.
        allow_negative_stock=True,
        allow_price_override=allow_price_override,
    )
    return {"sale_id": str(sale.pk), "number": sale.number, "total": str(sale.total)}


def _handle_sale_cancel(*, organization, device, operation, user, **_):
    payload = operation.get("payload") or {}
    # A malformed payload must fail this one operation, not the whole batch:
    # only DomainError / ValidationError are recorded as FAILED by process_one.
    try:
        sale = Sale.objects.get(pk=uuid.UUID(str(payload["sale"])))
    except (KeyError, ValueError, TypeError, Sale.DoesNotExist):
        raise InvalidOperation("Unknown or missing sale in the cancel payload.") from None
    cancelled = SaleService.cancel_sale(sale=sale, user=user, reason=payload.get("reason", ""))
    return {"sale_id": str(cancelled.pk), "status": cancelled.status}


def _handle_refund_create(*, organization, device, operation, user, **_):
    serializer = RefundCreateSerializer(data=operation.get("payload") or {})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    refund = RefundService.create_refund(
        sale=data["sale"],
        lines=data["lines"],
        user=user,
        method=data["method"],
        restock=data["restock"],
        reason=data.get("reason", ""),
        cash_register=data.get("cash_register") or device.cash_register,
        occurred_at=data.get("occurred_at") or operation.get("occurred_at"),
    )
    return {"refund_id": str(refund.pk), "number": refund.number, "total": str(refund.total)}


_HANDLERS = {
    SyncOperation.Type.SALE_CREATE: _handle_sale_create,
    SyncOperation.Type.SALE_CANCEL: _handle_sale_cancel,
    SyncOperation.Type.REFUND_CREATE: _handle_refund_create,
}


def pull_changes(*, organization, since=None, location=None) -> dict:
    """Everything a terminal needs to keep working offline, changed since `since`.

    Soft deletion is what makes this simple: nothing is ever removed, rows are
    deactivated, so there are no tombstones to reconcile. A terminal that
    receives `is_active: false` stops offering the item.
    """
    from apps.catalog.models import Brand, Category, Product, ProductVariant
    from apps.customers.models import Customer
    from apps.inventory.models import StockLevel

    cursor = timezone.now()

    def changed(queryset):
        return queryset.filter(updated_at__gt=since) if since else queryset

    stock = StockLevel.objects.select_related("variant")
    if location is not None:
        stock = stock.filter(location=location)

    return {
        "cursor": cursor,
        "since": since,
        "categories": changed(Category.objects.all()),
        "brands": changed(Brand.objects.all()),
        "products": changed(Product.objects.all()),
        "variants": changed(ProductVariant.objects.select_related("product")),
        "customers": changed(Customer.objects.all()),
        "stock": changed(stock),
    }
