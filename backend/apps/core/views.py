from __future__ import annotations

from rest_framework import viewsets
from rest_framework.response import Response

from .idempotency import IDEMPOTENCY_HEADER, ReplayedResponse, idempotent
from .permissions import HasCapability, HasOrganization, SubscriptionGrantsAccess


class TenantViewSetMixin:
    """Shared behaviour for every tenant-scoped endpoint.

    The queryset is already filtered by the TenantManager; this mixin adds the
    capability check and injects the organization on write.
    """

    permission_classes = [HasOrganization, HasCapability, SubscriptionGrantsAccess]
    read_capability: str | None = None
    write_capability: str | None = None
    capability_overrides: dict[str, str] = {}

    # Declared as a model + hints rather than a class-level `queryset`, because
    # a class attribute would be evaluated at import time, when no tenant
    # context exists yet.
    model = None
    select_related: tuple[str, ...] = ()
    prefetch_related: tuple[str, ...] = ()

    def get_queryset(self):
        queryset = self.model._default_manager.all()
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        if self.prefetch_related:
            queryset = queryset.prefetch_related(*self.prefetch_related)
        return queryset

    @property
    def organization(self):
        return self.request.organization

    def perform_create(self, serializer):
        serializer.save(organization=self.request.organization)


class ActiveByDefaultMixin:
    """Hides soft-deleted (`is_active=False`) rows unless asked for explicitly.

    A row stays in the database after "delete" because sales/inventory history
    references it, but a plain list call should not resurface it. Pass
    `?is_active=false` (or any `is_active` filter) or `?include_inactive=true`
    to see it anyway, e.g. for reports.

    `active_field` lets a view whose own model has no flag follow the one on
    what it describes: a stock row is not deactivated, the variant it counts
    is.
    """

    active_field = "is_active"

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        wants_inactive = "is_active" in params or params.get(
            "include_inactive", ""
        ).lower() in ("1", "true", "yes")
        if not wants_inactive:
            queryset = queryset.filter(**{self.active_field: True})
        return queryset


class TenantModelViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    pass


class TenantReadOnlyViewSet(TenantViewSetMixin, viewsets.ReadOnlyModelViewSet):
    pass


class IdempotentActionMixin:
    """Adds `Idempotency-Key` support to a custom write action.

    `idempotency_required` makes the header mandatory (used for money-moving
    operations in phase 2); when optional and absent, the action runs normally.
    """

    idempotency_required = False

    def run_idempotent(self, request, endpoint: str, handler):
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if not key:
            if self.idempotency_required:
                from rest_framework import status

                return Response(
                    {
                        "detail": f"{IDEMPOTENCY_HEADER} header is required for this operation.",
                        "code": "idempotency_key_required",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return handler()

        try:
            with idempotent(
                organization=request.organization,
                key=key,
                endpoint=endpoint,
                payload=request.data,
            ) as record:
                response = handler()
                record.set_response(response.status_code, response.data)
                return response
        except ReplayedResponse as replay:
            return Response(replay.body, status=replay.status_code, headers={"Idempotent-Replay": "true"})
