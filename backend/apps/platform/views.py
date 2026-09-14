from __future__ import annotations

import csv
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Membership
from apps.accounts.tokens import PLATFORM_SCOPE, issue_platform_tokens
from apps.catalog.models import Product
from apps.core.exceptions import InvalidOperation
from apps.core.permissions import IsPlatformStaff
from apps.organizations.models import Location, Organization
from apps.organizations.services import provision_organization
from apps.sales.models import Sale
from apps.subscriptions.models import Plan, Subscription, SubscriptionPayment
from apps.subscriptions.services import expire_if_needed, record_payment

User = get_user_model()



# ── Serializers ──────────────────────────────────────────────────────────────


class PlatformLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField()


class PlatformSubscriptionSerializer(serializers.ModelSerializer):
    grants_access = serializers.BooleanField(read_only=True)
    is_usable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            "id",
            "status",
            "billing_cycle",
            "trial_ends_at",
            "current_period_start",
            "current_period_end",
            "cancelled_at",
            "grants_access",
            "is_usable",
        ]


class PlatformPaymentSerializer(serializers.ModelSerializer):
    recorded_by_email = serializers.EmailField(source="recorded_by.email", read_only=True, default=None)

    class Meta:
        model = SubscriptionPayment
        fields = [
            "id",
            "amount",
            "currency",
            "paid_at",
            "period_start",
            "period_end",
            "method",
            "reference",
            "notes",
            "recorded_by",
            "recorded_by_email",
            "created_at",
        ]
        read_only_fields = ["id", "recorded_by", "recorded_by_email", "created_at"]


class PlatformPaymentCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"))
    currency = serializers.CharField(max_length=3, default="COP", required=False)
    paid_at = serializers.DateTimeField(required=False)
    period_start = serializers.DateTimeField()
    period_end = serializers.DateTimeField()
    method = serializers.ChoiceField(
        choices=SubscriptionPayment.Method.choices,
        default=SubscriptionPayment.Method.TRANSFER,
        required=False,
    )
    reference = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class PlatformOrgCreateSerializer(serializers.Serializer):
    organization_name = serializers.CharField(max_length=140)
    legal_name = serializers.CharField(max_length=180, required=False, allow_blank=True, default="")
    tax_id = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    owner_email = serializers.EmailField()
    owner_password = serializers.CharField(min_length=10)
    owner_first_name = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")
    owner_last_name = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")
    owner_username = serializers.CharField(max_length=40, required=False, allow_blank=True)
    months = serializers.IntegerField(min_value=1, default=1, required=False)
    trial_days = serializers.IntegerField(min_value=1, required=False, allow_null=True)


class PlatformOrgPatchSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=140, required=False)
    legal_name = serializers.CharField(max_length=180, required=False, allow_blank=True)
    tax_id = serializers.CharField(max_length=40, required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)


class PlatformSubscriptionPatchSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Subscription.Status.choices, required=False)
    current_period_end = serializers.DateTimeField(required=False, allow_null=True)
    trial_ends_at = serializers.DateTimeField(required=False, allow_null=True)


class PlatformMemberSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True, default=None)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    user_id = serializers.UUIDField(source="user.id", read_only=True)

    class Meta:
        model = Membership
        fields = [
            "id",
            "user_id",
            "email",
            "first_name",
            "last_name",
            "username",
            "role",
            "status",
            "created_at",
        ]


class PlatformMemberPatchSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Membership.Status.choices)


class PlatformSetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(min_length=10)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _usage_for(organization: Organization) -> dict:
    users_count = Membership.objects.filter(
        organization=organization, status=Membership.Status.ACTIVE
    ).count()
    locations_count = Location.all_objects.filter(
        organization=organization, is_active=True
    ).count()
    products_count = Product.all_objects.filter(
        organization=organization, is_active=True
    ).count()

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sales_qs = Sale.all_objects.filter(
        organization=organization,
        status__in=Sale.SETTLED_STATUSES,
        occurred_at__gte=month_start,
    )
    agg = sales_qs.aggregate(
        sales_month_count=Count("id"),
        sales_month_gross=Coalesce(Sum("total"), Decimal("0")),
    )
    return {
        "users_count": users_count,
        "locations_count": locations_count,
        "products_count": products_count,
        "sales_month_count": agg["sales_month_count"] or 0,
        "sales_month_gross": str(agg["sales_month_gross"] or Decimal("0")),
    }


def _owner_membership(organization: Organization) -> Membership | None:
    return (
        Membership.objects.filter(organization=organization, role=Membership.Role.OWNER)
        .select_related("user")
        .order_by("created_at")
        .first()
    )


def _serialize_org(organization: Organization, *, detail: bool = False) -> dict:
    sub = Subscription.all_objects.select_related("plan").filter(organization=organization).first()
    if sub is not None:
        expire_if_needed(sub)
        sub.refresh_from_db()
    owner = _owner_membership(organization)
    last_payment = (
        SubscriptionPayment.objects.filter(organization=organization)
        .order_by("-paid_at")
        .first()
    )
    payload = {
        "id": str(organization.id),
        "name": organization.name,
        "slug": organization.slug,
        "legal_name": organization.legal_name,
        "tax_id": organization.tax_id,
        "is_active": organization.is_active,
        "created_at": organization.created_at,
        "owner": (
            {
                "membership_id": str(owner.id),
                "user_id": str(owner.user_id),
                "email": owner.user.email,
                "username": owner.username,
                "first_name": owner.user.first_name,
                "last_name": owner.user.last_name,
            }
            if owner
            else None
        ),
        "subscription": PlatformSubscriptionSerializer(sub).data if sub else None,
        "last_payment": PlatformPaymentSerializer(last_payment).data if last_payment else None,
        "payment_status": (
            "ok"
            if organization.is_active and sub is not None and sub.grants_access
            else ("suspended" if not organization.is_active else "overdue")
        ),
    }
    if detail:
        payload["usage"] = _usage_for(organization)
        payload["payments"] = PlatformPaymentSerializer(
            SubscriptionPayment.objects.filter(organization=organization)[:50],
            many=True,
        ).data
    return payload


# ── Views ────────────────────────────────────────────────────────────────────


class PlatformLoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_scope = "auth"

    @extend_schema(request=PlatformLoginSerializer, responses={200: None})
    def post(self, request):
        serializer = PlatformLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().lower()
        password = serializer.validated_data["password"]

        user = User.objects.filter(email=email, is_staff=True).first()
        if (
            user is None
            or not user.check_password(password)
            or not user.is_active
            or Membership.objects.filter(user=user).exists()
        ):
            return Response(
                {"detail": "Invalid credentials.", "code": "invalid_credentials"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        tokens = issue_platform_tokens(user)
        return Response(
            {
                **tokens,
                "scope": PLATFORM_SCOPE,
                "user": {
                    "id": str(user.id),
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                },
            }
        )





class PlatformOrganizationViewSet(viewsets.ViewSet):
    permission_classes = [IsPlatformStaff]

    def list(self, request):
        qs = Organization.objects.all().order_by("-created_at")
        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(slug__icontains=search)
                | Q(tax_id__icontains=search)
                | Q(memberships__user__email__icontains=search)
            ).distinct()

        is_active = request.query_params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() in ("1", "true", "yes"))

        sub_status = request.query_params.get("subscription_status")
        if sub_status:
            qs = qs.filter(subscription__status=sub_status)

        results = [_serialize_org(org) for org in qs[:200]]
        return Response({"count": len(results), "results": results})

    def retrieve(self, request, pk=None):
        org = Organization.objects.filter(pk=pk).first()
        if org is None:
            return Response({"detail": "Not found.", "code": "not_found"}, status=404)
        return Response(_serialize_org(org, detail=True))

    def create(self, request):
        serializer = PlatformOrgCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        email = data["owner_email"].strip().lower()
        if User.objects.filter(email=email).exists():
            return Response(
                {"detail": "Ya existe una cuenta con ese correo.", "code": "email_taken"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        plan = Plan.objects.filter(code=Plan.Code.BASIC).first()
        if plan is None:
            return Response(
                {"detail": "Configuración de acceso incompleta.", "code": "access_config_missing"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=data["owner_password"],
                first_name=data.get("owner_first_name", ""),
                last_name=data.get("owner_last_name", ""),
            )
            membership = provision_organization(
                user=user,
                name=data["organization_name"],
                legal_name=data.get("legal_name", ""),
                tax_id=data.get("tax_id", ""),
                username=data.get("owner_username") or None,
                plan=plan,
                plan_code=plan.code,
                months=data.get("months", 1),
                trial_days=data.get("trial_days"),
                use_trial=False,
            )

        return Response(
            _serialize_org(membership.organization, detail=True),
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, pk=None):
        org = Organization.objects.filter(pk=pk).first()
        if org is None:
            return Response({"detail": "Not found.", "code": "not_found"}, status=404)
        serializer = PlatformOrgPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        for field in ("name", "legal_name", "tax_id", "is_active"):
            if field in data:
                setattr(org, field, data[field])
        org.save()
        return Response(_serialize_org(org, detail=True))

    @action(detail=True, methods=["patch"], url_path="subscription")
    def subscription(self, request, pk=None):
        org = Organization.objects.filter(pk=pk).first()
        if org is None:
            return Response({"detail": "Not found.", "code": "not_found"}, status=404)
        sub = Subscription.all_objects.filter(organization=org).first()
        if sub is None:
            return Response({"detail": "No subscription.", "code": "not_found"}, status=404)

        serializer = PlatformSubscriptionPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if "status" in data:
            sub.status = data["status"]
        if "current_period_end" in data:
            sub.current_period_end = data["current_period_end"]
        if "trial_ends_at" in data:
            sub.trial_ends_at = data["trial_ends_at"]
        sub.save()
        return Response(PlatformSubscriptionSerializer(sub).data)

    @action(detail=True, methods=["get", "post"], url_path="payments")
    def payments(self, request, pk=None):
        org = Organization.objects.filter(pk=pk).first()
        if org is None:
            return Response({"detail": "Not found.", "code": "not_found"}, status=404)

        if request.method == "GET":
            qs = SubscriptionPayment.objects.filter(organization=org)
            return Response(PlatformPaymentSerializer(qs, many=True).data)

        serializer = PlatformPaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        payment = record_payment(
            organization=org,
            amount=data["amount"],
            currency=data.get("currency", "COP"),
            paid_at=data.get("paid_at"),
            period_start=data["period_start"],
            period_end=data["period_end"],
            method=data.get("method", SubscriptionPayment.Method.TRANSFER),
            reference=data.get("reference", ""),
            notes=data.get("notes", ""),
            recorded_by=request.user,
        )
        return Response(PlatformPaymentSerializer(payment).data, status=201)

    @action(detail=True, methods=["get"], url_path="members")
    def members(self, request, pk=None):
        org = Organization.objects.filter(pk=pk).first()
        if org is None:
            return Response({"detail": "Not found.", "code": "not_found"}, status=404)
        qs = Membership.objects.filter(organization=org).select_related("user").order_by("role", "username")
        return Response(PlatformMemberSerializer(qs, many=True).data)

    @action(detail=True, methods=["patch"], url_path=r"members/(?P<member_id>[^/.]+)")
    def member_patch(self, request, pk=None, member_id=None):
        org = Organization.objects.filter(pk=pk).first()
        if org is None:
            return Response({"detail": "Not found.", "code": "not_found"}, status=404)
        membership = Membership.objects.filter(organization=org, pk=member_id).select_related("user").first()
        if membership is None:
            return Response({"detail": "Not found.", "code": "not_found"}, status=404)

        serializer = PlatformMemberPatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_status = serializer.validated_data["status"]

        if (
            membership.role == Membership.Role.OWNER
            and new_status != Membership.Status.ACTIVE
        ):
            other_owners = Membership.objects.filter(
                organization=org,
                role=Membership.Role.OWNER,
                status=Membership.Status.ACTIVE,
            ).exclude(pk=membership.pk)
            if not other_owners.exists():
                raise InvalidOperation("Cannot deactivate the last active owner.")

        membership.status = new_status
        membership.save(update_fields=["status", "updated_at"])
        return Response(PlatformMemberSerializer(membership).data)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"members/(?P<member_id>[^/.]+)/set-password",
    )
    def member_set_password(self, request, pk=None, member_id=None):
        org = Organization.objects.filter(pk=pk).first()
        if org is None:
            return Response({"detail": "Not found.", "code": "not_found"}, status=404)
        membership = Membership.objects.filter(organization=org, pk=member_id).select_related("user").first()
        if membership is None:
            return Response({"detail": "Not found.", "code": "not_found"}, status=404)

        serializer = PlatformSetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership.user.set_password(serializer.validated_data["password"])
        membership.user.save(update_fields=["password"])
        return Response({"detail": "Password updated.", "code": "ok"})

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        qs = Organization.objects.all().order_by("name")
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "id",
                "name",
                "slug",
                "tax_id",
                "is_active",
                "owner_email",
                "subscription_status",
                "period_end",
                "payment_status",
                "grants_access",
            ]
        )
        for org in qs:
            sub = Subscription.all_objects.select_related("plan").filter(organization=org).first()
            if sub:
                expire_if_needed(sub)
                sub.refresh_from_db()
            owner = _owner_membership(org)
            grants = bool(sub and sub.grants_access and org.is_active)
            writer.writerow(
                [
                    str(org.id),
                    org.name,
                    org.slug,
                    org.tax_id,
                    org.is_active,
                    owner.user.email if owner else "",
                    sub.status if sub else "",
                    sub.current_period_end.isoformat() if sub and sub.current_period_end else "",
                    "ok" if grants else ("suspended" if not org.is_active else "overdue"),
                    grants,
                ]
            )
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="organizations.csv"'
        return response
