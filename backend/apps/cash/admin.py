from django.contrib import admin

from apps.core.admin import ReadOnlyAdminMixin, UnscopedTenantAdmin

from .models import CashMovement, CashRegister, CashSession


@admin.register(CashRegister)
class CashRegisterAdmin(UnscopedTenantAdmin):
    list_display = ("name", "code", "organization", "location", "is_active")
    list_filter = ("is_active", "organization", "location")
    search_fields = ("name", "code", "organization__name", "location__name")
    autocomplete_fields = ("organization", "location")
    select_related_fields = ("organization", "location")


@admin.register(CashSession)
class CashSessionAdmin(UnscopedTenantAdmin):
    list_display = (
        "register",
        "organization",
        "status",
        "opened_by",
        "opened_at",
        "opening_amount",
        "closed_at",
        "counted_amount",
        "difference",
        "close_reason",
    )
    list_filter = ("status", "close_reason", "organization", "register")
    search_fields = ("register__name", "register__code", "organization__name", "notes")
    autocomplete_fields = (
        "organization",
        "register",
        "opened_by",
        "closed_by",
        "previous_session",
        "superseded_by",
    )
    select_related_fields = ("organization", "register", "opened_by", "closed_by")
    date_hierarchy = "opened_at"
    readonly_fields = ("expected_amount", "difference")


@admin.register(CashMovement)
class CashMovementAdmin(ReadOnlyAdminMixin, UnscopedTenantAdmin):
    list_display = (
        "created_at",
        "session",
        "organization",
        "movement_type",
        "amount",
        "source_type",
        "source_id",
        "created_by",
    )
    list_filter = ("movement_type", "organization")
    search_fields = ("note", "source_id", "session__register__code", "organization__name")
    select_related_fields = ("organization", "session", "session__register", "created_by")
    readonly_fields = [f.name for f in CashMovement._meta.fields]
    date_hierarchy = "created_at"

    def has_change_permission(self, request, obj=None):
        return False
