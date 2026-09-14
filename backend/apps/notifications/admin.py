from django.contrib import admin

from apps.core.admin import UnscopedTenantAdmin

from .models import WebPushSubscription


@admin.register(WebPushSubscription)
class WebPushSubscriptionAdmin(UnscopedTenantAdmin):
    list_display = ("id", "organization", "user", "created_at")
    list_filter = ("organization",)
    search_fields = ("endpoint", "user__email", "organization__name")
    autocomplete_fields = ("organization", "user")
    select_related_fields = ("organization", "user")
    readonly_fields = ("created_at", "updated_at")
