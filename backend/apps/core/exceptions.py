"""Domain exceptions and the single API error contract."""
from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


class TenantContextMissing(RuntimeError):
    """Raised when tenant-scoped data is queried without an active organization.

    This is a programming error, not a client error: it means a code path
    escaped the request context. It must fail loudly instead of silently
    returning an empty queryset, which would hide the bug.
    """


class DomainError(Exception):
    """Base class for business-rule violations surfaced to the API."""

    default_message = "Business rule violation."
    code = "domain_error"
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, message: str | None = None, **context):
        self.message = message or self.default_message
        self.context = context
        super().__init__(self.message)


class InsufficientStock(DomainError):
    default_message = "Not enough stock available."
    code = "insufficient_stock"
    status_code = status.HTTP_409_CONFLICT


class InvalidOperation(DomainError):
    default_message = "This operation is not allowed in the current state."
    code = "invalid_operation"


class SharedIdentity(DomainError):
    """A business tried to edit personal data of someone who also works elsewhere."""

    default_message = "That person also works in another business; only they can change this."
    code = "shared_identity"
    status_code = status.HTTP_403_FORBIDDEN


class IdempotencyConflict(DomainError):
    default_message = "This idempotency key was already used with a different payload."
    code = "idempotency_conflict"
    status_code = status.HTTP_409_CONFLICT


class OperationInProgress(DomainError):
    default_message = "An identical request is currently being processed."
    code = "operation_in_progress"
    status_code = status.HTTP_409_CONFLICT


class SubscriptionInactive(DomainError):
    default_message = "This organization's subscription is not active."
    code = "subscription_inactive"
    status_code = status.HTTP_403_FORBIDDEN


class OrganizationSuspended(DomainError):
    default_message = "This organization has been suspended."
    code = "organization_suspended"
    status_code = status.HTTP_403_FORBIDDEN


class PlanLimitExceeded(DomainError):
    default_message = "Your current plan does not allow this."
    code = "plan_limit_exceeded"
    status_code = status.HTTP_402_PAYMENT_REQUIRED


def api_exception_handler(exc, context):
    """Map domain errors onto a stable JSON error envelope."""
    if isinstance(exc, DomainError):
        return Response(
            {"detail": exc.message, "code": exc.code, "context": exc.context},
            status=exc.status_code,
        )

    response = drf_exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict) and "code" not in response.data:
        response.data["code"] = getattr(exc, "default_code", "error")
    return response
