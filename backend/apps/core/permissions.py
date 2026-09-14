"""Server-side authorization. The frontend hiding a button is never the control."""
from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .exceptions import OrganizationSuspended, SubscriptionInactive


class HasOrganization(BasePermission):
    """Requires a token belonging to an active account inside a business."""

    message = "No active organization in this session."

    def has_permission(self, request, view):
        return getattr(request, "organization", None) is not None


class HasCapability(BasePermission):
    """Checks the capability the view declares for the current action.

    Views declare `read_capability` / `write_capability`, optionally overridden
    per action via `capability_overrides = {"destroy": "products.write"}`.
    """

    message = "Your role does not allow this action."

    def has_permission(self, request, view):
        membership = getattr(request, "membership", None)
        if membership is None:
            return False

        required = self._required_capability(request, view)
        if required is None:
            return True
        return membership.has_capability(required)

    @staticmethod
    def _required_capability(request, view) -> str | None:
        overrides = getattr(view, "capability_overrides", {}) or {}
        action = getattr(view, "action", None)
        if action and action in overrides:
            return overrides[action]
        if request.method in SAFE_METHODS:
            return getattr(view, "read_capability", None)
        return getattr(view, "write_capability", None)


class SubscriptionGrantsAccess(BasePermission):
    """Hard-blocks every tenant request when the subscription does not grant access.

    Replaces the old writes-only gate: unpaid / expired shops cannot read or
    write until a platform operator records a payment (or reactivates them).
    """

    def has_permission(self, request, view):
        organization = getattr(request, "organization", None)
        if organization is None:
            return True

        if not organization.is_active:
            raise OrganizationSuspended(
                f"The organization {organization.name} has been suspended.",
            )

        subscription = getattr(request, "_subscription", None)
        if subscription is None:
            from apps.subscriptions.models import Subscription
            from apps.subscriptions.services import expire_if_needed

            subscription = Subscription.all_objects.filter(organization=organization).first()
            if subscription is not None:
                subscription = expire_if_needed(subscription)
            request._subscription = subscription

        if subscription is not None and not subscription.grants_access:
            raise SubscriptionInactive(
                f"The subscription for {organization.name} is {subscription.status.lower()}.",
                status=subscription.status,
            )
        return True


# Backwards-compatible name used by older imports / docs.
SubscriptionAllowsWrites = SubscriptionGrantsAccess


class IsPlatformStaff(BasePermission):
    """Platform operators: is_staff, no tenant memberships."""

    message = "Platform operator access required."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        if not getattr(user, "is_staff", False):
            return False
        # Platform operators are never members of a business.
        from apps.accounts.models import Membership

        return not Membership.objects.filter(user=user).exists()
