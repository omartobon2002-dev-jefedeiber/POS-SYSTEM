from django.contrib import admin

from apps.core.admin import UnscopedTenantAdmin

from .models import Customer


@admin.register(Customer)
class CustomerAdmin(UnscopedTenantAdmin):
    list_display = (
        "name",
        "organization",
        "document_type",
        "document_number",
        "phone",
        "email",
        "is_active",
    )
    list_filter = ("is_active", "document_type", "organization")
    search_fields = ("name", "document_number", "phone", "email", "organization__name")
    autocomplete_fields = ("organization",)
