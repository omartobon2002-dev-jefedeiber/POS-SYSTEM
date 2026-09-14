from django.contrib import admin

from apps.core.admin import ReadOnlyAdminMixin, UnscopedTenantAdmin

from .models import Payment, Refund, RefundItem, Sale, SaleItem


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    fields = (
        "sku",
        "description",
        "quantity",
        "unit_price",
        "discount_amount",
        "line_total",
        "tax_amount",
        "refunded_quantity",
    )
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("method", "amount", "reference", "created_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class RefundItemInline(admin.TabularInline):
    model = RefundItem
    extra = 0
    fields = ("sale_item", "quantity", "amount")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Sale)
class SaleAdmin(ReadOnlyAdminMixin, UnscopedTenantAdmin):
    list_display = (
        "number",
        "organization",
        "location",
        "status",
        "customer",
        "seller",
        "total",
        "occurred_at",
        "source",
    )
    list_filter = ("status", "source", "organization", "location")
    search_fields = ("number", "customer__name", "organization__name", "device_id", "notes")
    select_related_fields = (
        "organization",
        "location",
        "customer",
        "seller",
        "cash_session",
        "cancelled_by",
    )
    readonly_fields = [f.name for f in Sale._meta.fields]
    inlines = [SaleItemInline, PaymentInline]
    date_hierarchy = "occurred_at"

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SaleItem)
class SaleItemAdmin(ReadOnlyAdminMixin, UnscopedTenantAdmin):
    list_display = ("sale", "sku", "description", "quantity", "unit_price", "line_total", "organization")
    list_filter = ("organization",)
    search_fields = ("sku", "description", "sale__number")
    select_related_fields = ("organization", "sale", "variant")
    readonly_fields = [f.name for f in SaleItem._meta.fields]

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(ReadOnlyAdminMixin, UnscopedTenantAdmin):
    list_display = ("sale", "method", "amount", "reference", "organization", "created_at")
    list_filter = ("method", "organization")
    search_fields = ("sale__number", "reference")
    select_related_fields = ("organization", "sale")
    readonly_fields = [f.name for f in Payment._meta.fields]

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Refund)
class RefundAdmin(ReadOnlyAdminMixin, UnscopedTenantAdmin):
    list_display = (
        "number",
        "sale",
        "organization",
        "location",
        "total",
        "method",
        "occurred_at",
        "created_by",
    )
    list_filter = ("method", "organization", "location")
    search_fields = ("number", "sale__number", "reason", "organization__name")
    select_related_fields = ("organization", "sale", "location", "cash_session", "created_by")
    readonly_fields = [f.name for f in Refund._meta.fields]
    inlines = [RefundItemInline]
    date_hierarchy = "occurred_at"

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(RefundItem)
class RefundItemAdmin(ReadOnlyAdminMixin, UnscopedTenantAdmin):
    list_display = ("refund", "sale_item", "quantity", "amount", "organization")
    list_filter = ("organization",)
    search_fields = ("refund__number", "sale_item__sku")
    select_related_fields = ("organization", "refund", "sale_item")
    readonly_fields = [f.name for f in RefundItem._meta.fields]

    def has_change_permission(self, request, obj=None):
        return False
