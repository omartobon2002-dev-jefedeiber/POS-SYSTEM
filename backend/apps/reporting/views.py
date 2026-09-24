from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core import capabilities as caps
from apps.core.params import parse_datetime_param, parse_int_param, parse_uuid_param
from apps.core.permissions import HasCapability, HasOrganization
from apps.organizations.models import Location

from . import selectors
from .serializers import (
    CashSessionsReportSerializer,
    DashboardSerializer,
    ExpensesSummarySerializer,
    InventoryValuationSerializer,
    MarginSerializer,
    ProfitSerializer,
    RefundsSummarySerializer,
    ReportIndexSerializer,
    SalesSummarySerializer,
    TopProductSerializer,
)

PERIOD_PARAMS = [
    OpenApiParameter("from", str, description="ISO-8601 start. Defaults to 30 days ago."),
    OpenApiParameter("to", str, description="ISO-8601 end. Defaults to now."),
    OpenApiParameter("location", str, description="Restrict to one location."),
]


class ReportViewSet(viewsets.ViewSet):
    """Read-only aggregates. Every figure is derived, never stored."""

    permission_classes = [HasOrganization, HasCapability]
    read_capability = caps.REPORTS_READ
    write_capability = caps.REPORTS_READ
    serializer_class = ReportIndexSerializer

    def _period(self, request):
        return selectors.resolve_period(
            parse_datetime_param(request.query_params.get("from"), "from"),
            parse_datetime_param(request.query_params.get("to"), "to"),
        )

    def _location(self, request):
        location_id = parse_uuid_param(request.query_params.get("location"), "location")
        return Location.objects.filter(pk=location_id).first() if location_id else None

    @extend_schema(parameters=PERIOD_PARAMS, responses={200: SalesSummarySerializer})
    @action(detail=False, methods=["get"], url_path="sales-summary")
    def sales_summary(self, request):
        date_from, date_to = self._period(request)
        return Response(
            selectors.sales_summary(
                date_from=date_from, date_to=date_to, location=self._location(request)
            )
        )

    @extend_schema(
        parameters=PERIOD_PARAMS + [OpenApiParameter("limit", int, description="Default 10.")],
        responses={200: TopProductSerializer(many=True)},
    )
    @action(detail=False, methods=["get"], url_path="top-products")
    def top_products(self, request):
        date_from, date_to = self._period(request)
        limit = parse_int_param(request.query_params.get("limit"), "limit", default=10, maximum=100)
        return Response(
            selectors.top_products(
                date_from=date_from,
                date_to=date_to,
                location=self._location(request),
                limit=limit,
            )
        )

    @extend_schema(parameters=PERIOD_PARAMS, responses={200: MarginSerializer})
    @action(detail=False, methods=["get"])
    def margin(self, request):
        date_from, date_to = self._period(request)
        return Response(
            selectors.margin_report(
                date_from=date_from, date_to=date_to, location=self._location(request)
            )
        )

    @extend_schema(
        parameters=[OpenApiParameter("location", str)],
        responses={200: InventoryValuationSerializer},
    )
    @action(detail=False, methods=["get"], url_path="inventory-valuation")
    def inventory_valuation(self, request):
        return Response(selectors.inventory_valuation(location=self._location(request)))

    @extend_schema(parameters=PERIOD_PARAMS, responses={200: CashSessionsReportSerializer})
    @action(detail=False, methods=["get"], url_path="cash-sessions")
    def cash_sessions(self, request):
        date_from, date_to = self._period(request)
        return Response(
            selectors.cash_sessions_report(
                date_from=date_from, date_to=date_to, location=self._location(request)
            )
        )

    @extend_schema(parameters=PERIOD_PARAMS, responses={200: RefundsSummarySerializer})
    @action(detail=False, methods=["get"])
    def refunds(self, request):
        date_from, date_to = self._period(request)
        return Response(
            selectors.refunds_summary(
                date_from=date_from, date_to=date_to, location=self._location(request)
            )
        )

    @extend_schema(parameters=PERIOD_PARAMS, responses={200: ExpensesSummarySerializer})
    @action(detail=False, methods=["get"])
    def expenses(self, request):
        date_from, date_to = self._period(request)
        return Response(
            selectors.expenses_summary(
                date_from=date_from, date_to=date_to, location=self._location(request)
            )
        )

    @extend_schema(parameters=PERIOD_PARAMS, responses={200: ProfitSerializer})
    @action(detail=False, methods=["get"])
    def profit(self, request):
        """Revenue, cost of goods and operating expenses down to what is left."""
        date_from, date_to = self._period(request)
        return Response(
            selectors.profit_and_loss(
                date_from=date_from, date_to=date_to, location=self._location(request)
            )
        )

    @extend_schema(
        parameters=PERIOD_PARAMS
        + [OpenApiParameter("top_limit", int, description="Top products to include. Default 5.")],
        responses={200: DashboardSerializer},
    )
    @action(detail=False, methods=["get"])
    def dashboard(self, request):
        """Every headline figure of the reports page, in one request."""
        date_from, date_to = self._period(request)
        top_limit = parse_int_param(
            request.query_params.get("top_limit"), "top_limit", default=5, maximum=50
        )
        return Response(
            selectors.dashboard(
                date_from=date_from,
                date_to=date_to,
                location=self._location(request),
                top_limit=top_limit,
            )
        )

    @extend_schema(responses={200: ReportIndexSerializer})
    def list(self, request):
        """Index of the available reports, so a client can discover them."""
        return Response(
            {
                "reports": [
                    "dashboard",
                    "sales-summary",
                    "top-products",
                    "margin",
                    "expenses",
                    "profit",
                    "inventory-valuation",
                    "cash-sessions",
                    "refunds",
                ]
            }
        )
