from django.contrib import admin

from apps.core.admin import UnscopedTenantAdmin

from .models import Expense, ExpenseCategory


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(UnscopedTenantAdmin):
    list_display = ("name", "organization", "is_active")
    list_filter = ("is_active", "organization")
    search_fields = ("name", "organization__name")
    autocomplete_fields = ("organization",)


@admin.register(Expense)
class ExpenseAdmin(UnscopedTenantAdmin):
    list_display = (
        "occurred_at",
        "organization",
        "location",
        "category",
        "description",
        "amount",
        "payment_method",
        "cash_session",
    )
    list_filter = ("payment_method", "organization", "location", "category")
    search_fields = ("description", "reference", "note", "organization__name")
    autocomplete_fields = (
        "organization",
        "location",
        "category",
        "supplier",
        "cash_session",
        "created_by",
    )
    select_related_fields = (
        "organization",
        "location",
        "category",
        "supplier",
        "cash_session",
        "created_by",
    )
    date_hierarchy = "occurred_at"
