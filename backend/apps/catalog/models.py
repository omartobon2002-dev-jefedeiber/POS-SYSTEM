"""Fashion retail catalogue.

The core decision here is that stock never lives on Product. A shoe is not
sellable; a shoe in size 40, black, is. So Product carries identity and
marketing data, ProductVariant carries everything transactional: SKU, barcode,
price, cost and inventory.
"""
from __future__ import annotations

import uuid

from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TenantScopedModel


def product_photo_path(instance: Product, filename: str) -> str:
    """One photo per product. A random name avoids leaking the original
    filename and colliding with a re-upload while the old file is deleted."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"products/{instance.organization_id}/{uuid.uuid4().hex}.{extension}"


class Category(TenantScopedModel):
    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="children"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "categories"
        ordering = ["name"]
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "parent", "name"], name="uq_category_org_parent_name"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Brand(TenantScopedModel):
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "brands"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uq_brand_org_name"),
        ]

    def __str__(self) -> str:
        return self.name


class Product(TenantScopedModel):
    """The commercial concept ("Nike Air Max"). Never sold or stocked directly."""

    name = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="products"
    )
    brand = models.ForeignKey(
        Brand, on_delete=models.SET_NULL, null=True, blank=True, related_name="products"
    )
    track_inventory = models.BooleanField(
        default=True, help_text="False for services or made-to-order items."
    )
    is_active = models.BooleanField(default=True)
    # One photo, not a gallery: a small shop photographs a garment once and
    # that is what customers and staff need to recognise it on a shelf or a
    # receipt. Uploaded and replaced through `POST /products/{id}/photo/`,
    # never through the plain create/update body.
    image = models.ImageField(upload_to=product_photo_path, null=True, blank=True)

    class Meta:
        db_table = "products"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["organization", "is_active"]),
            models.Index(fields=["organization", "name"]),
        ]

    def __str__(self) -> str:
        return self.name


class ProductVariant(TenantScopedModel):
    """The sellable, stockable unit.

    `size` and `color` are explicit columns because they are the two axes every
    fashion retailer actually filters and reports on. `attributes` absorbs the
    long tail (material, season, fit) without an EAV schema or a migration per
    new attribute.
    """

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    sku = models.CharField(max_length=64)
    barcode = models.CharField(max_length=64, blank=True, default="")
    size = models.CharField(max_length=30, blank=True, default="")
    color = models.CharField(max_length=40, blank=True, default="")
    attributes = models.JSONField(default=dict, blank=True)

    # Tax-inclusive shelf price (decision D1).
    price = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(0)])
    # Moving weighted average, recomputed on every purchase receipt (decision D3).
    average_cost = models.DecimalField(
        max_digits=14, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    last_purchase_cost = models.DecimalField(
        max_digits=14, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )

    weight_grams = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "product_variants"
        ordering = ["product__name", "size", "color"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "sku"], name="uq_variant_org_sku"),
            models.UniqueConstraint(
                fields=["organization", "barcode"],
                condition=~models.Q(barcode=""),
                name="uq_variant_org_barcode",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "sku"]),
            models.Index(fields=["organization", "barcode"]),
            models.Index(fields=["organization", "is_active"]),
        ]

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        parts = [self.product.name, self.size, self.color]
        return " / ".join(p for p in parts if p)
