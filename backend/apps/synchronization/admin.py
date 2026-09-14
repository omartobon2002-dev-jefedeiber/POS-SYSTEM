from django.contrib import admin

from apps.core.admin import ReadOnlyAdminMixin, UnscopedTenantAdmin

from .models import Device, SyncOperation


@admin.register(Device)
class DeviceAdmin(UnscopedTenantAdmin):
    list_display = (
        "name",
        "identifier",
        "organization",
        "location",
        "platform",
        "is_active",
        "last_seen_at",
        "last_sync_at",
    )
    list_filter = ("is_active", "platform", "organization", "location")
    search_fields = ("name", "identifier", "organization__name")
    autocomplete_fields = ("organization", "location", "cash_register", "registered_by")
    select_related_fields = ("organization", "location", "cash_register", "registered_by")
    # Token is hashed; never show it in the change form.
    exclude = ("token",)
    readonly_fields = ("token_issued_at", "last_seen_at", "last_sync_at")


@admin.register(SyncOperation)
class SyncOperationAdmin(ReadOnlyAdminMixin, UnscopedTenantAdmin):
    list_display = (
        "received_at",
        "organization",
        "device",
        "operation_type",
        "status",
        "operation_id",
        "error_code",
        "occurred_at",
    )
    list_filter = ("status", "operation_type", "organization")
    search_fields = ("operation_id", "error_code", "error_detail", "device__identifier")
    select_related_fields = ("organization", "device", "processed_by")
    readonly_fields = [f.name for f in SyncOperation._meta.fields]
    date_hierarchy = "received_at"

    def has_change_permission(self, request, obj=None):
        return False
