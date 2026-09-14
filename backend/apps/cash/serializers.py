from __future__ import annotations

from rest_framework import serializers

from apps.core.fields import TenantPrimaryKeyRelatedField
from apps.core.serializers import TenantModelSerializer

from .models import CashMovement, CashMovementType, CashRegister, CashSession


class CashRegisterSerializer(TenantModelSerializer):
    location_name = serializers.CharField(source="location.name", read_only=True)
    open_session = serializers.SerializerMethodField()

    class Meta:
        model = CashRegister
        fields = ["id", "location", "location_name", "name", "code", "is_active", "open_session"]
        read_only_fields = ["id"]

    def get_open_session(self, obj) -> str | None:
        session = obj.sessions.filter(status=CashSession.Status.OPEN).first()
        return str(session.pk) if session else None


class CashMovementSerializer(TenantModelSerializer):
    created_by_email = serializers.CharField(source="created_by.email", read_only=True, default=None)

    class Meta:
        model = CashMovement
        fields = [
            "id",
            "session",
            "movement_type",
            "amount",
            "source_type",
            "source_id",
            "created_by",
            "created_by_email",
            "note",
            "created_at",
        ]
        read_only_fields = fields


class CashSessionSerializer(TenantModelSerializer):
    register_name = serializers.CharField(source="register.name", read_only=True)
    opened_by_email = serializers.CharField(source="opened_by.email", read_only=True, default=None)

    class Meta:
        model = CashSession
        fields = [
            "id",
            "register",
            "register_name",
            "status",
            "opened_by",
            "opened_by_email",
            "opened_at",
            "opening_amount",
            "closed_by",
            "closed_at",
            "expected_amount",
            "counted_amount",
            "difference",
            "close_reason",
            "previous_session",
            "superseded_by",
            "notes",
        ]
        read_only_fields = fields


class OpenSessionSerializer(serializers.Serializer):
    register = TenantPrimaryKeyRelatedField(queryset=CashRegister.objects)
    opening_amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0, default=0)
    notes = serializers.CharField(required=False, allow_blank=True)


class CloseSessionSerializer(serializers.Serializer):
    counted_amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    notes = serializers.CharField(required=False, allow_blank=True)


class TransferSessionSerializer(serializers.Serializer):
    counted_amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    to_user = serializers.UUIDField()
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_to_user(self, value):
        from apps.accounts.models import User

        try:
            return User.objects.get(pk=value)
        except User.DoesNotExist as exc:
            raise serializers.ValidationError("User not found.") from exc


class TransferSessionResultSerializer(serializers.Serializer):
    closed_session = CashSessionSerializer()
    opened_session = CashSessionSerializer()


class CashMovementInputSerializer(serializers.Serializer):
    """Withdrawals and deposits. Amount is always positive; the type gives the sign."""

    movement_type = serializers.ChoiceField(
        choices=[
            (CashMovementType.WITHDRAWAL, "Withdrawal"),
            (CashMovementType.DEPOSIT, "Deposit"),
            (CashMovementType.ADJUSTMENT, "Adjustment"),
        ]
    )
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    note = serializers.CharField(max_length=240, required=False, allow_blank=True)

    def validate(self, attrs):
        if attrs["movement_type"] != CashMovementType.ADJUSTMENT and attrs["amount"] <= 0:
            raise serializers.ValidationError(
                {"amount": "Use a positive amount; the movement type decides the direction."}
            )
        if attrs["amount"] == 0:
            raise serializers.ValidationError({"amount": "A cash movement cannot be zero."})
        return attrs
