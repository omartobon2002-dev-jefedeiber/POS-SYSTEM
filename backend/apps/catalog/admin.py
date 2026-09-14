from django.contrib import admin

from apps.core.admin import UnscopedTenantAdmin

from .models import Brand, Category, Product, ProductVariant


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0
    fields = ("sku", "barcode", "size", "color", "price", "is_active")
    show_change_link = True
    fk_name = "product"


@admin.register(Category)
class CategoryAdmin(UnscopedTenantAdmin):
    list_display = ("name", "organization", "parent", "is_active")
    list_filter = ("is_active", "organization")
    search_fields = ("name", "organization__name")
    autocomplete_fields = ("organization", "parent")
    select_related_fields = ("organization", "parent")


@admin.register(Brand)
class BrandAdmin(UnscopedTenantAdmin):
    list_display = ("name", "organization", "is_active")
    list_filter = ("is_active", "organization")
    search_fields = ("name", "organization__name")
    autocomplete_fields = ("organization",)


@admin.register(Product)
class ProductAdmin(UnscopedTenantAdmin):
    list_display = ("name", "organization", "category", "brand", "is_active", "track_inventory")
    list_filter = ("is_active", "track_inventory", "organization")
    search_fields = ("name", "organization__name", "variants__sku")
    autocomplete_fields = ("organization", "category", "brand")
    select_related_fields = ("organization", "category", "brand")
    inlines = [ProductVariantInline]


@admin.register(ProductVariant)
class ProductVariantAdmin(UnscopedTenantAdmin):
    list_display = ("sku", "product", "organization", "size", "color", "price", "is_active")
    list_filter = ("is_active", "organization")
    search_fields = ("sku", "barcode", "product__name", "organization__name")
    autocomplete_fields = ("organization", "product")
    select_related_fields = ("organization", "product")
