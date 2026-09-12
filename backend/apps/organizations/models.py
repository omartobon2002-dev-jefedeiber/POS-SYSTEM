"""The tenant itself and its physical locations.

People are not modelled here: an account belongs to exactly one business and
lives in apps.accounts as `User.organization`. There is no membership table
because there is nothing to join - identity is `(organization, username)`.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify

from apps.core.models import TenantScopedModel, TimeStampedModel, UUIDModel


class TaxRegime(models.TextChoices):
    """DIAN standing of the business. Printed on the receipt, never calculated with."""

    RESPONSABLE_IVA = "RESPONSABLE_IVA", "Responsable de IVA"
    NO_RESPONSABLE_IVA = "NO_RESPONSABLE_IVA", "No responsable de IVA"
    REGIMEN_SIMPLE = "REGIMEN_SIMPLE", "Régimen Simple de Tributación"


class ReceiptPaperWidth(models.TextChoices):
    MM80 = "80mm", "80 mm"
    MM58 = "58mm", "58 mm"


class Organization(UUIDModel, TimeStampedModel):
    """A business. This model is the tenant, so it is not tenant-scoped itself.

    Business configuration lives here rather than in a separate settings table:
    it is a single row per tenant and splitting it would only add a join.
    """

    name = models.CharField(max_length=140)
    slug = models.SlugField(max_length=160, unique=True)
    legal_name = models.CharField(max_length=180, blank=True)
    tax_id = models.CharField(max_length=40, blank=True, help_text="NIT / RUT.")

    phone = models.CharField(max_length=30, blank=True)
    address = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=120, blank=True)

    country = models.CharField(max_length=2, default="CO")
    currency = models.CharField(max_length=3, default="COP")
    currency_decimals = models.PositiveSmallIntegerField(
        default=0, help_text="Smallest circulating unit. COP has no cents."
    )
    timezone = models.CharField(max_length=64, default="America/Bogota")

    # Tax is configured once per business and nowhere else: whether it sells
    # with IVA at all, and at what rate. Products no longer carry their own
    # rate. Prices stay tax-inclusive (decision D1), so the tax is extracted
    # from the price when documenting the sale, never added on top.
    charges_tax = models.BooleanField(
        default=True, help_text="Does this business sell with IVA?"
    )
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=19,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Percentage. Ignored while charges_tax is False.",
    )
    tax_regime = models.CharField(
        max_length=20, choices=TaxRegime.choices, default=TaxRegime.RESPONSABLE_IVA
    )

    dian_resolution = models.CharField(max_length=120, blank=True)
    dian_resolution_range = models.CharField(max_length=120, blank=True)
    dian_resolution_valid_until = models.DateField(null=True, blank=True)
    receipt_footer = models.TextField(blank=True)
    receipt_paper_width = models.CharField(
        max_length=4, choices=ReceiptPaperWidth.choices, default=ReceiptPaperWidth.MM80
    )

    is_active = models.BooleanField(default=True)

    @property
    def effective_tax_rate(self) -> Decimal:
        """The only rate a sale may be built with. Zero means the tax split is a no-op.

        Turning `charges_tax` off deliberately leaves `tax_rate` untouched, so
        switching it back on restores the rate the business had configured.
        """
        return self.tax_rate if self.charges_tax else Decimal("0")

    class Meta:
        db_table = "organizations"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:150] or "org"
            slug, suffix = base, 1
            while Organization.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                suffix += 1
                slug = f"{base}-{suffix}"[:160]
            self.slug = slug
        super().save(*args, **kwargs)


class Location(TenantScopedModel):
    """A store or warehouse.

    Decision D2: modelled from day one even though the founding customer has a
    single store. Inventory movements, cash sessions and sales all carry a
    location, so adding a second store later is inserting rows, not migrating
    history.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="locations")
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=20)
    address = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "locations"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uq_location_org_code"),
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(is_default=True),
                name="uq_location_one_default_per_org",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"
