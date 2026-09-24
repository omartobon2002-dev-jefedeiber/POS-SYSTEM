from __future__ import annotations

from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import Count
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core import capabilities as caps
from apps.core.views import IdempotentActionMixin, TenantViewSetMixin
from apps.organizations.selectors import default_location

from .filters import RefundFilter, SaleFilter
from .models import Refund, Sale
from .serializers import (
    RefundCreateSerializer,
    RefundSerializer,
    SaleCancelSerializer,
    SaleCreateSerializer,
    SaleListSerializer,
    SaleSerializer,
)
from .services import RefundService, SaleService


class SaleViewSet(
    IdempotentActionMixin,
    TenantViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Sales are created and cancelled, never edited.

    A completed sale is an accounting fact. Correcting it means a refund or a
    cancellation, both of which leave a trail.
    """

    model = Sale
    select_related = ("customer", "seller", "location", "cash_session")
    read_capability = caps.SALES_READ
    write_capability = caps.SALES_CREATE
    capability_overrides = {"cancel": caps.SALES_CANCEL}
    filterset_class = SaleFilter
    search_fields = ["number", "customer__name", "items__sku"]
    ordering_fields = ["occurred_at", "total", "created_at"]

    # Money must not move twice because a till lost its connection mid-request.
    idempotency_required = True
    throttle_scope = "write"

    def get_serializer_class(self):
        if self.action == "list":
            return SaleListSerializer
        if self.action == "create":
            return SaleCreateSerializer
        return SaleSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == "list":
            # The aggregate introduces a GROUP BY, which drops Meta.ordering and
            # would make pagination non-deterministic.
            return queryset.annotate(
                item_count=Count("items", distinct=True),
                payment_methods=ArrayAgg("payments__method", distinct=True),
            ).order_by("-occurred_at", "-created_at")
        return queryset.prefetch_related(
            "items", "payments", "refunds", "refunds__items"
        )

    @extend_schema(request=SaleCreateSerializer, responses={201: SaleSerializer})
    def create(self, request):
        return self.run_idempotent(
            request, endpoint="sale.create", handler=lambda: self._create(request)
        )

    def _create(self, request):
        serializer = SaleCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        location = data.get("location") or default_location()
        if location is None:
            return Response(
                {"detail": "This organization has no active location.", "code": "no_location"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sale = SaleService.create_sale(
            organization=request.organization,
            location=location,
            lines=data["lines"],
            payments=data["payments"],
            user=request.user,
            customer=data["customer"],
            cash_register=data.get("cash_register"),
            occurred_at=data.get("occurred_at"),
            notes=data.get("notes", ""),
            expected_total=data.get("expected_total"),
            sale_id=data.get("id"),
            allow_price_override=request.membership.has_capability(caps.SALES_OVERRIDE_PRICE),
        )
        return Response(
            SaleSerializer(self.get_queryset().get(pk=sale.pk)).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(request=SaleCancelSerializer, responses={200: SaleSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        serializer = SaleCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sale = SaleService.cancel_sale(
            sale=self.get_object(),
            user=request.user,
            reason=serializer.validated_data.get("reason", ""),
        )
        return Response(SaleSerializer(self.get_queryset().get(pk=sale.pk)).data)

    @extend_schema(responses={200: None})
    @action(detail=True, methods=["get"])
    def receipt(self, request, pk=None):
        """Everything a printed receipt needs, with the tax broken out per rate."""
        sale = self.get_object()
        by_rate: dict[str, dict] = {}
        for item in sale.items.all():
            bucket = by_rate.setdefault(
                str(item.tax_rate), {"tax_rate": item.tax_rate, "base": 0, "tax": 0}
            )
            bucket["base"] += item.taxable_base
            bucket["tax"] += item.tax_amount

        return Response(
            {
                "sale": SaleSerializer(sale).data,
                "organization": {
                    "name": request.organization.name,
                    "legal_name": request.organization.legal_name,
                    "tax_id": request.organization.tax_id,
                },
                "tax_breakdown": list(by_rate.values()),
            }
        )


class RefundViewSet(
    IdempotentActionMixin,
    TenantViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = RefundSerializer
    model = Refund
    select_related = ("sale", "location", "created_by", "cash_session")
    prefetch_related = ("items", "items__sale_item")
    read_capability = caps.SALES_READ
    write_capability = caps.SALES_REFUND
    filterset_class = RefundFilter
    search_fields = ["number", "sale__number", "reason"]
    ordering_fields = ["occurred_at", "total"]
    idempotency_required = True
    throttle_scope = "write"

    def get_serializer_class(self):
        return RefundCreateSerializer if self.action == "create" else RefundSerializer

    @extend_schema(request=RefundCreateSerializer, responses={201: RefundSerializer})
    def create(self, request):
        return self.run_idempotent(
            request, endpoint="refund.create", handler=lambda: self._create(request)
        )

    def _create(self, request):
        serializer = RefundCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        refund = RefundService.create_refund(
            sale=data["sale"],
            lines=data["lines"],
            user=request.user,
            method=data["method"],
            restock=data["restock"],
            reason=data.get("reason", ""),
            cash_register=data.get("cash_register"),
            occurred_at=data.get("occurred_at"),
        )
        return Response(
            RefundSerializer(self.get_queryset().get(pk=refund.pk)).data,
            status=status.HTTP_201_CREATED,
        )
