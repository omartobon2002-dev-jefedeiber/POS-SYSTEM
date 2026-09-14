"""Cash control: registers, sessions and drawer movements.

Only cash movements are recorded here. Card and transfer payments belong to the
sale; mixing them into the drawer would make every arqueo show a difference
that does not exist.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.models import TenantScopedModel


class CashRegister(TenantScopedModel):
    """A physical till. Belongs to a location, may be used by many cashiers."""

    location = models.ForeignKey(
        "organizations.Location", on_delete=models.CASCADE, related_name="cash_registers"
    )
    name = models.CharField(max_length=80)
    code = models.CharField(max_length=20)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "cash_registers"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uq_cash_register_org_code"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class CashSession(TenantScopedModel):
    """One shift on one register: opened with a float, closed with a count."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    class CloseReason(models.TextChoices):
        NORMAL = "NORMAL", "Normal close"
        TRANSFER = "TRANSFER", "Handed off to another cashier"

    register = models.ForeignKey(CashRegister, on_delete=models.PROTECT, related_name="sessions")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="opened_sessions"
    )
    opened_at = models.DateTimeField()
    opening_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_sessions",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    # Filled at closing: what the system says, what the cashier counted, and the gap.
    expected_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    counted_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    difference = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    # Blank while OPEN; set on close. TRANSFER allows a same-day reopen for the next cashier.
    close_reason = models.CharField(
        max_length=10, choices=CloseReason.choices, blank=True, default=""
    )
    # When this shift was opened via a handoff, points at the session that was transferred.
    previous_session = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="successor_sessions",
    )
    # When closed via TRANSFER, points at the session that took over.
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supersedes",
    )

    notes = models.TextField(blank=True)

    class Meta:
        db_table = "cash_sessions"
        ordering = ["-opened_at"]
        constraints = [
            # A register can only have one shift open at a time.
            models.UniqueConstraint(
                fields=["register"],
                condition=models.Q(status="OPEN"),
                name="uq_cash_session_one_open_per_register",
            ),
        ]
        indexes = [models.Index(fields=["organization", "status", "-opened_at"])]

    def __str__(self) -> str:
        return f"{self.register_id} {self.status} {self.opened_at:%Y-%m-%d %H:%M}"

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.OPEN


class CashMovementType(models.TextChoices):
    OPENING = "OPENING", "Opening float"
    SALE = "SALE", "Sale"
    REFUND = "REFUND", "Refund"
    WITHDRAWAL = "WITHDRAWAL", "Withdrawal"
    DEPOSIT = "DEPOSIT", "Deposit"
    ADJUSTMENT = "ADJUSTMENT", "Adjustment"


class CashMovement(TenantScopedModel):
    """Signed money in or out of the drawer. Append-only, like the stock ledger."""

    session = models.ForeignKey(CashSession, on_delete=models.CASCADE, related_name="movements")
    movement_type = models.CharField(max_length=20, choices=CashMovementType.choices, db_index=True)
    amount = models.DecimalField(
        max_digits=14, decimal_places=2, help_text="Signed: positive enters the drawer."
    )
    source_type = models.CharField(max_length=30, blank=True, db_index=True)
    source_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="cash_movements"
    )
    note = models.CharField(max_length=240, blank=True)

    class Meta:
        db_table = "cash_movements"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["organization", "session", "created_at"]),
            models.Index(fields=["organization", "source_type", "source_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.movement_type} {self.amount:+}"

# Module-level alias so drf-spectacular can name this enum in the OpenAPI
# schema; its override loader cannot traverse into a nested class.
CASH_SESSION_STATUS_CHOICES = CashSession.Status.choices
CASH_SESSION_CLOSE_REASON_CHOICES = CashSession.CloseReason.choices
