"""SaaS billing domain.

Deliberately *not* integrated with a payment provider yet. The model captures
plan, status, trial and billing cycle, which is what the product needs today;
a provider (Wompi, Mercado Pago, Stripe) is added later behind the
`provider` / `external_reference` fields without touching the core domain.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TenantScopedModel, TimeStampedModel, UUIDModel


class Plan(UUIDModel, TimeStampedModel):
    """Platform-level catalogue of plans. Shared by all tenants, so not scoped."""

    class Code(models.TextChoices):
        BASIC = "BASIC", "Basic"
        PRO = "PRO", "Pro"
        BUSINESS = "BUSINESS", "Business"

    code = models.CharField(max_length=20, choices=Code.choices, unique=True)
    name = models.CharField(max_length=60)
    description = models.CharField(max_length=240, blank=True)
    monthly_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    yearly_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="COP")

    # Null means unlimited.
    max_users = models.PositiveIntegerField(null=True, blank=True)
    max_locations = models.PositiveIntegerField(null=True, blank=True)
    max_products = models.PositiveIntegerField(null=True, blank=True)

    features = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "plans"
        ordering = ["sort_order", "monthly_price"]

    def __str__(self) -> str:
        return self.name


class Subscription(TenantScopedModel):
    class Status(models.TextChoices):
        TRIAL = "TRIAL", "Trial"
        ACTIVE = "ACTIVE", "Active"
        PAST_DUE = "PAST_DUE", "Past due"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"

    class BillingCycle(models.TextChoices):
        MONTHLY = "MONTHLY", "Monthly"
        YEARLY = "YEARLY", "Yearly"

    organization = models.OneToOneField(
        "organizations.Organization", on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TRIAL)
    billing_cycle = models.CharField(
        max_length=10, choices=BillingCycle.choices, default=BillingCycle.MONTHLY
    )
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    # Payment-provider agnostic hooks. Empty until a gateway is integrated.
    provider = models.CharField(max_length=30, blank=True)
    external_reference = models.CharField(max_length=120, blank=True)

    class Meta:
        db_table = "subscriptions"

    def __str__(self) -> str:
        return f"{self.organization_id} - {self.plan_id} ({self.status})"

    @property
    def grants_access(self) -> bool:
        """Whether the tenant may log in and use the product at all.

        PAST_DUE / CANCELLED / EXPIRED never grant access. ACTIVE and TRIAL
        require their period/trial end to still be in the future when set.
        """
        now = timezone.now()
        if self.status == self.Status.ACTIVE:
            if self.current_period_end is not None and self.current_period_end <= now:
                return False
            return True
        if self.status == self.Status.TRIAL:
            return self.trial_ends_at is None or self.trial_ends_at > now
        return False

    @property
    def is_usable(self) -> bool:
        """Alias of grants_access — kept for existing serializers/clients."""
        return self.grants_access


class SubscriptionPayment(UUIDModel, TimeStampedModel):
    """Manual SaaS payment recorded by a platform operator."""

    class Method(models.TextChoices):
        TRANSFER = "TRANSFER", "Transferencia"
        CASH = "CASH", "Efectivo"
        OTHER = "OTHER", "Otro"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="subscription_payments",
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="payments",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="COP")
    paid_at = models.DateTimeField()
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.TRANSFER)
    reference = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_subscription_payments",
    )

    class Meta:
        db_table = "subscription_payments"
        ordering = ["-paid_at", "-created_at"]

    def __str__(self) -> str:
        return f"{self.organization_id} {self.amount} {self.currency} ({self.paid_at:%Y-%m-%d})"


# Module-level alias so drf-spectacular can name this enum in the OpenAPI
# schema; its override loader cannot traverse into a nested class.
SUBSCRIPTION_STATUS_CHOICES = Subscription.Status.choices
PAYMENT_METHOD_CHOICES = SubscriptionPayment.Method.choices
