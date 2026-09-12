from __future__ import annotations

from rest_framework import serializers

from apps.core.serializers import TenantModelSerializer

from .models import Location, Organization


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = [
            "id",
            "name",
            "slug",
            "legal_name",
            "tax_id",
            "phone",
            "address",
            "city",
            "country",
            "currency",
            "currency_decimals",
            "timezone",
            "charges_tax",
            "tax_rate",
            "tax_regime",
            "dian_resolution",
            "dian_resolution_range",
            "dian_resolution_valid_until",
            "receipt_footer",
            "receipt_paper_width",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["id", "slug", "is_active", "created_at"]

    def validate_tax_rate(self, value):
        # The model validators do not run on their own in DRF, and a rate
        # outside 0-100 would silently corrupt every sale that follows.
        if value < 0 or value > 100:
            raise serializers.ValidationError("La tarifa de IVA debe estar entre 0 y 100.")
        return value


class LocationSerializer(TenantModelSerializer):
    class Meta:
        model = Location
        fields = ["id", "name", "code", "address", "phone", "is_default", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]
