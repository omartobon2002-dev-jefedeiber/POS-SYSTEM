from django.contrib import admin

from apps.core.admin import UnscopedTenantAdmin

from .models import Purchase, PurchaseItem, Supplier


class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 0
    fields = ("variant", "quantity", "unit_cost", "total_cost")
    autocomplete_fields = ("variant",)
    show_change_link = True


@admin.register(Supplier)
class SupplierAdmin(UnscopedTenantAdmin):
    list_display = ("name", "organization", "tax_id", "phone", "email", "is_active")
    list_filter = ("is_active", "organization")
    search_fields = ("name", "tax_id", "email", "phone", "organization__name")
    autocomplete_fields = ("organization",)


@admin.register(Purchase)
class PurchaseAdmin(UnscopedTenantAdmin):
    list_display = (
        "number",
        "organization",
        "location",
        "supplier",
        "status",
        "total_cost",
        "purchased_at",
        "received_at",
    )
    list_filter = ("status", "organization", "location")
    search_fields = ("number", "supplier__name", "organization__name", "supplier_invoice")
    autocomplete_fields = ("organization", "location", "supplier", "created_by", "received_by")
    select_related_fields = ("organization", "location", "supplier", "created_by", "received_by")
    inlines = [PurchaseItemInline]
    date_hierarchy = "purchased_at"


@admin.register(PurchaseItem)
class PurchaseItemAdmin(UnscopedTenantAdmin):
    list_display = ("purchase", "variant", "organization", "quantity", "unit_cost", "total_cost")
    list_filter = ("organization",)
    search_fields = ("purchase__number", "variant__sku")
    autocomplete_fields = ("organization", "purchase", "variant")
    select_related_fields = ("organization", "purchase", "variant")
