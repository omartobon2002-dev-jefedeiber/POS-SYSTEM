from django.contrib import admin

from apps.core.admin import UnscopedTenantAdmin

from .models import Plan, Subscription, SubscriptionPayment


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "monthly_price",
        "yearly_price",
        "currency",
        "max_users",
        "max_locations",
        "is_active",
        "sort_order",
    )
    list_filter = ("is_active", "code")
    search_fields = ("code", "name")
    ordering = ("sort_order", "monthly_price")


@admin.register(Subscription)
class SubscriptionAdmin(UnscopedTenantAdmin):
    list_display = (
        "organization",
        "plan",
        "status",
        "billing_cycle",
        "trial_ends_at",
        "current_period_end",
        "provider",
    )
    list_filter = ("status", "billing_cycle", "plan")
    search_fields = ("organization__name", "organization__slug", "external_reference")
    autocomplete_fields = ("organization", "plan")
    select_related_fields = ("organization", "plan")


@admin.register(SubscriptionPayment)
class SubscriptionPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "paid_at",
        "organization",
        "subscription",
        "amount",
        "currency",
        "method",
        "reference",
        "recorded_by",
    )
    list_filter = ("method", "currency", "organization")
    search_fields = ("reference", "notes", "organization__name")
    autocomplete_fields = ("organization", "subscription", "recorded_by")
    date_hierarchy = "paid_at"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "organization", "subscription", "subscription__plan", "recorded_by"
        )
