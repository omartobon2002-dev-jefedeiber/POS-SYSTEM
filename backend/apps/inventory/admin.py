from django.contrib import admin

from apps.core.admin import ReadOnlyAdminMixin, UnscopedTenantAdmin

from .models import InventoryMovement, StockDiscrepancy, StockLevel


@admin.register(StockLevel)
class StockLevelAdmin(UnscopedTenantAdmin):
    list_display = ("variant", "location", "organization", "quantity", "reorder_point", "updated_at")
    list_filter = ("organization", "location")
    search_fields = ("variant__sku", "variant__product__name", "location__name", "organization__name")
    autocomplete_fields = ("organization", "location", "variant")
    select_related_fields = ("organization", "location", "variant", "variant__product")
    readonly_fields = ("quantity",)


@admin.register(InventoryMovement)
class InventoryMovementAdmin(ReadOnlyAdminMixin, UnscopedTenantAdmin):
    list_display = (
        "created_at",
        "organization",
        "location",
        "variant",
        "movement_type",
        "quantity",
        "source_type",
        "source_id",
    )
    list_filter = ("movement_type", "organization", "location")
    search_fields = ("variant__sku", "source_id", "note", "organization__name")
    select_related_fields = ("organization", "location", "variant", "created_by")
    readonly_fields = [f.name for f in InventoryMovement._meta.fields]
    date_hierarchy = "created_at"

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(StockDiscrepancy)
class StockDiscrepancyAdmin(UnscopedTenantAdmin):
    list_display = (
        "created_at",
        "organization",
        "location",
        "variant",
        "quantity_before",
        "quantity_requested",
        "quantity_after",
        "is_resolved",
        "resolved_at",
    )
    list_filter = ("is_resolved", "organization", "location")
    search_fields = ("variant__sku", "organization__name", "reason", "source_id")
    select_related_fields = ("organization", "location", "variant")
    autocomplete_fields = ("organization", "location", "variant")
    date_hierarchy = "created_at"
