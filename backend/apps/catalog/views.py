from __future__ import annotations

from django.db.models import Count, Sum
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.core import capabilities as caps
from apps.core.audit import record_audit
from apps.core.views import ActiveByDefaultMixin, TenantModelViewSet
from apps.inventory.services import zero_out_stock
from apps.subscriptions.limits import enforce_limit

from .filters import CategoryFilter, ProductFilter, ProductVariantFilter
from .models import Brand, Category, Product, ProductVariant
from .selectors import variants_pending_cost
from .serializers import (
    BrandSerializer,
    CategorySerializer,
    ProductPhotoSerializer,
    ProductSerializer,
    ProductVariantSerializer,
    SetVariantCostSerializer,
)


class CategoryViewSet(TenantModelViewSet):
    serializer_class = CategorySerializer
    model = Category
    read_capability = caps.PRODUCTS_READ
    write_capability = caps.PRODUCTS_WRITE
    filterset_class = CategoryFilter
    search_fields = ["name"]


class BrandViewSet(TenantModelViewSet):
    serializer_class = BrandSerializer
    model = Brand
    read_capability = caps.PRODUCTS_READ
    write_capability = caps.PRODUCTS_WRITE
    filterset_fields = ["is_active"]
    search_fields = ["name"]


class ProductViewSet(ActiveByDefaultMixin, TenantModelViewSet):
    """The commercial concept, with its variants written inline.

    Deleting deactivates: sales and the inventory ledger point here and must
    stay readable. A deactivated product drops out of this list and its stock
    is taken down to zero; `?include_inactive=true` brings it back for reports.
    """

    serializer_class = ProductSerializer
    model = Product
    select_related = ("category", "brand")
    prefetch_related = ("variants",)
    read_capability = caps.PRODUCTS_READ
    write_capability = caps.PRODUCTS_WRITE
    filterset_class = ProductFilter
    search_fields = ["name", "description", "variants__sku", "variants__barcode"]
    ordering_fields = ["name", "created_at"]

    def perform_create(self, serializer):
        enforce_limit(
            organization=self.request.organization,
            resource="products",
            current_count=Product.objects.count(),
        )
        super().perform_create(serializer)
        record_audit(
            organization=self.request.organization,
            action="product.created",
            actor=self.request.user,
            obj=serializer.instance,
            metadata={"name": serializer.instance.name},
        )

    def perform_destroy(self, instance):
        # Products are referenced by sales and the inventory ledger.
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        variants = list(instance.variants.all())
        instance.variants.update(is_active=False)
        for variant in variants:
            zero_out_stock(organization=self.request.organization, variant=variant, user=self.request.user)

    @extend_schema(
        request={"multipart/form-data": ProductPhotoSerializer},
        responses={200: ProductSerializer},
    )
    @action(
        detail=True,
        methods=["post", "delete"],
        parser_classes=[MultiPartParser, FormParser],
    )
    def photo(self, request, pk=None):
        """Upload/replace (POST) or remove (DELETE) the product's one photo.

        Separate from the plain create/update body on purpose: a file does not
        travel as JSON, and every other product field should stay editable
        without ever needing multipart form data.
        """
        product = self.get_object()

        if request.method == "DELETE":
            if product.image:
                product.image.delete(save=False)
                product.image = None
                product.save(update_fields=["image", "updated_at"])
            return Response(self.get_serializer(product).data)

        serializer = ProductPhotoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if product.image:
            # Old file is not referenced anywhere else, so it is deleted
            # before the new one is stored - otherwise every re-upload would
            # leak an orphaned file on disk.
            product.image.delete(save=False)
        product.image = serializer.validated_data["image"]
        product.save(update_fields=["image", "updated_at"])

        return Response(self.get_serializer(product).data, status=status.HTTP_200_OK)


class ProductVariantViewSet(ActiveByDefaultMixin, TenantModelViewSet):
    """The sellable, stockable unit: a size and colour of a product.

    Deleting deactivates it and zeroes its stock, same as with a product;
    `?include_inactive=true` still lists it.
    """

    serializer_class = ProductVariantSerializer
    model = ProductVariant
    select_related = ("product", "product__brand")
    read_capability = caps.PRODUCTS_READ
    write_capability = caps.PRODUCTS_WRITE
    filterset_class = ProductVariantFilter
    search_fields = ["sku", "barcode", "product__name", "size", "color"]
    ordering_fields = ["sku", "price", "created_at"]

    @extend_schema(
        parameters=[OpenApiParameter("barcode", str, required=True)],
        responses={200: ProductVariantSerializer},
    )
    @action(detail=False, methods=["get"])
    def lookup(self, request):
        """Barcode scan endpoint for the POS. Also matches on exact SKU."""
        code = (request.query_params.get("barcode") or "").strip()
        if not code:
            return Response({"detail": "barcode is required.", "code": "missing_barcode"}, status=400)

        variant = self.get_queryset().filter(barcode=code).first() or self.get_queryset().filter(
            sku=code
        ).first()
        if variant is None:
            return Response({"detail": "No variant matches that code.", "code": "not_found"}, status=404)
        return Response(self.get_serializer(variant).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        totals = self.get_queryset().aggregate(
            variants=Count("id"), retail_value=Sum("price")
        )
        return Response(totals)

    @extend_schema(
        responses={
            200: {
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "results": {"type": "array", "items": {"type": "object"}},
                },
            }
        }
    )
    @action(detail=False, methods=["get"], url_path="pending-cost")
    def pending_cost(self, request):
        """Variants the owner still needs to price for margin (costs.read)."""
        if not request.membership.has_capability(caps.COSTS_READ):
            return Response(
                {"detail": "You do not have permission to perform this action."},
                status=status.HTTP_403_FORBIDDEN,
            )
        queryset = variants_pending_cost()
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page if page is not None else queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response({"count": queryset.count(), "results": serializer.data})

    @extend_schema(request=SetVariantCostSerializer, responses={200: ProductVariantSerializer})
    @action(detail=True, methods=["post"], url_path="set-cost")
    def set_cost(self, request, pk=None):
        """Set average_cost without changing stock quantity (costs.read)."""
        if not request.membership.has_capability(caps.COSTS_READ):
            return Response(
                {"detail": "You do not have permission to perform this action."},
                status=status.HTTP_403_FORBIDDEN,
            )
        variant = self.get_object()
        serializer = SetVariantCostSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        unit_cost = serializer.validated_data["unit_cost"]
        variant.average_cost = unit_cost
        variant.last_purchase_cost = unit_cost
        variant.save(update_fields=["average_cost", "last_purchase_cost", "updated_at"])
        record_audit(
            organization=request.organization,
            action="variant.cost_set",
            actor=request.user,
            obj=variant,
            metadata={"unit_cost": str(unit_cost)},
        )
        return Response(self.get_serializer(variant).data)

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        zero_out_stock(organization=self.request.organization, variant=instance, user=self.request.user)
