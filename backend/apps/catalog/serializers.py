from __future__ import annotations

from django.db import transaction
from rest_framework import serializers

from apps.core.serializers import TenantModelSerializer

from .models import Brand, Category, Product, ProductVariant


class CategorySerializer(TenantModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "parent", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class BrandSerializer(TenantModelSerializer):
    class Meta:
        model = Brand
        fields = ["id", "name", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class ProductVariantSerializer(TenantModelSerializer):
    display_name = serializers.CharField(read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = ProductVariant
        fields = [
            "id",
            "product",
            "product_name",
            "display_name",
            "sku",
            "barcode",
            "size",
            "color",
            "attributes",
            "price",
            "average_cost",
            "last_purchase_cost",
            "weight_grams",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["id", "average_cost", "last_purchase_cost", "created_at"]


class NestedVariantSerializer(TenantModelSerializer):
    """Variants written inline when creating a product.

    Cost is exposed read-only: the catalogue list is where margins are read, and
    without it every product came back looking like it had no cost at all. It is
    never writable here — the server derives it from receipts (decision D3).
    """

    id = serializers.UUIDField(required=False)

    class Meta:
        model = ProductVariant
        fields = [
            "id",
            "sku",
            "barcode",
            "size",
            "color",
            "attributes",
            "price",
            "average_cost",
            "last_purchase_cost",
            "weight_grams",
            "is_active",
        ]
        read_only_fields = ["average_cost", "last_purchase_cost"]


MAX_PHOTO_SIZE = 4 * 1024 * 1024  # Under DATA_UPLOAD_MAX_MEMORY_SIZE (5MB), so
# an oversized file fails here with a clean message rather than as Django's
# generic "request body too large".
ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ProductPhotoSerializer(serializers.Serializer):
    image = serializers.ImageField()

    def validate_image(self, value):
        if value.size > MAX_PHOTO_SIZE:
            raise serializers.ValidationError(
                f"La imagen no puede pesar más de {MAX_PHOTO_SIZE // (1024 * 1024)}MB."
            )
        if value.content_type not in ALLOWED_PHOTO_TYPES:
            raise serializers.ValidationError("Formato no soportado. Usa JPEG, PNG o WEBP.")
        return value


class ProductSerializer(TenantModelSerializer):
    variants = NestedVariantSerializer(many=True, required=False)
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    brand_name = serializers.CharField(source="brand.name", read_only=True, default=None)
    # Read-only here on purpose: a file does not travel in the same JSON body
    # as the nested variants. Upload/replace through the dedicated `photo`
    # action, which accepts multipart form data.
    image = serializers.ImageField(read_only=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "description",
            "category",
            "category_name",
            "brand",
            "brand_name",
            "track_inventory",
            "is_active",
            "image",
            "variants",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_variants(self, value):
        skus = [v["sku"] for v in value if v.get("sku")]
        if len(skus) != len(set(skus)):
            raise serializers.ValidationError("Duplicate SKU in the submitted variants.")

        barcodes = [v["barcode"] for v in value if v.get("barcode")]
        if len(barcodes) != len(set(barcodes)):
            raise serializers.ValidationError("Duplicate barcode in the submitted variants.")

        # A SKU/barcode is unique across the whole business, not just within
        # this product - two products can otherwise land on the same code
        # (e.g. both built from "category + color + size") and the second one
        # would fail on the database constraint instead of a readable error.
        own_variant_ids = (
            {v.pk for v in self.instance.variants.all()} if self.instance is not None else set()
        )
        if skus:
            taken = set(
                ProductVariant.objects.filter(sku__in=skus)
                .exclude(pk__in=own_variant_ids)
                .values_list("sku", flat=True)
            )
            if taken:
                raise serializers.ValidationError(
                    {"sku": f"Ya existe en este negocio: {', '.join(sorted(taken))}."}
                )
        if barcodes:
            taken = set(
                ProductVariant.objects.filter(barcode__in=barcodes)
                .exclude(pk__in=own_variant_ids)
                .values_list("barcode", flat=True)
            )
            if taken:
                raise serializers.ValidationError(
                    {"barcode": f"Ya existe en este negocio: {', '.join(sorted(taken))}."}
                )
        return value

    @transaction.atomic
    def create(self, validated_data):
        variants = validated_data.pop("variants", [])
        product = super().create(validated_data)
        ProductVariant.objects.bulk_create(
            [
                ProductVariant(organization=product.organization, product=product, **variant)
                for variant in variants
            ]
        )
        return product

    @transaction.atomic
    def update(self, instance, validated_data):
        """Upsert semantics: variants with an id are updated, new ones created.

        Variants are never deleted here - they are referenced by inventory
        movements and sales. Deactivate instead.
        """
        variants = validated_data.pop("variants", None)
        product = super().update(instance, validated_data)
        if variants is None:
            return product

        existing = {str(v.pk): v for v in product.variants.all()}
        for payload in variants:
            variant_id = str(payload.pop("id", "") or "")
            if variant_id and variant_id in existing:
                variant = existing[variant_id]
                for field, value in payload.items():
                    setattr(variant, field, value)
                variant.save()
            else:
                ProductVariant.objects.create(
                    organization=product.organization, product=product, **payload
                )
        return product
