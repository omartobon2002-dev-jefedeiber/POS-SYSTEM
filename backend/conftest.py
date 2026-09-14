"""Shared fixtures.

Tenant-scoped models refuse to be read without an active organization, so
service-level tests run inside `tenant_context(...)` while API tests get the
context from the token, exactly like production does.

The identity model is hybrid: a `User` is a person (global, identified by
email) and a `Membership` is that person inside one business (username,
role). Fixtures return both, because tests need each for different reasons:
sales and cash sessions point at the person, authorization at the membership.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.accounts.tokens import issue_identity_tokens, issue_session_tokens
from apps.core.context import tenant_context
from apps.organizations.models import Location
from apps.organizations.services import provision_organization

User = get_user_model()


@pytest.fixture
def make_identity(db):
    """A person, with no business yet."""

    def _make(email: str | None = None, password: str = "ClaveDePrueba123", **extra):
        return User.objects.create_user(
            email=email or f"persona{uuid.uuid4().hex[:10]}@example.com",
            password=password,
            **extra,
        )

    return _make


@pytest.fixture
def make_tenant(db, make_identity):
    """Provision a complete business: owner, default location, trial plan."""

    def _make(name: str = "Tienda", username: str | None = None, owner=None):
        # Transactional tests truncate every table, including the plan rows
        # created by a data migration, so make sure they are there.
        from apps.subscriptions.management.commands.seed_plans import seed_plans

        seed_plans()

        owner = owner or make_identity()
        membership = provision_organization(user=owner, name=name, username=username)
        organization = membership.organization
        with tenant_context(organization.pk):
            location = Location.objects.get(is_default=True)
        return SimpleNamespace(
            org=organization, owner=owner, membership=membership, location=location
        )

    return _make


@pytest.fixture
def make_employee(db):
    """Someone on the team: an active membership inside one business.

    Returns the membership, which is what the API and the authorization layer
    both work with. The person behind it is `.user`.
    """

    def _make(
        tenant,
        role=Membership.Role.CASHIER,
        username=None,
        status=None,
        user=None,
        password="ClaveDePrueba123",
    ):
        # No email by default: this is the cashier created at the counter, who
        # has no global identity and never signs in with one.
        user = user or User.objects.create_user(email=None, first_name="Empleado", password=password)
        membership = Membership.objects.create(
            user=user,
            organization=tenant.org,
            username=username or f"emp{uuid.uuid4().hex[:8]}",
            role=role,
            status=status or Membership.Status.ACTIVE,
            default_location=tenant.location,
        )
        return membership

    return _make


@pytest.fixture
def join(db):
    """Add an existing person to another business: the point of the whole model."""

    def _join(tenant, user, role=Membership.Role.CASHIER, username=None):
        membership = Membership.objects.create(
            user=user,
            organization=tenant.org,
            username=username or f"emp{uuid.uuid4().hex[:8]}",
            role=role,
            status=Membership.Status.ACTIVE,
            default_location=tenant.location,
        )
        return membership

    return _join


@pytest.fixture
def tenant_a(make_tenant):
    return make_tenant("Moda Urbana")


@pytest.fixture
def tenant_b(make_tenant):
    return make_tenant("Otra Tienda")


@pytest.fixture
def client_for():
    """An APIClient with a session token for one business.

    Accepts either a membership or a person; with a person, `organization`
    picks which of their businesses the session is for, and may be omitted when
    they only have one.
    """

    def _client(subject, organization=None):
        membership = subject
        if isinstance(subject, User):
            memberships = Membership.objects.filter(user=subject)
            if organization is not None:
                memberships = memberships.filter(organization=organization)
            membership = memberships.get()

        client = APIClient()
        tokens = issue_session_tokens(membership)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        return client

    return _client


@pytest.fixture
def identity_client_for():
    """An APIClient holding only an identity token: it can choose, not operate."""

    def _client(user):
        client = APIClient()
        tokens = issue_identity_tokens(user)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        return client

    return _client


@pytest.fixture
def make_variant(db):
    """Create a product with one variant inside a tenant."""

    def _make(tenant, sku: str | None = None, price="100000.00"):
        from apps.catalog.models import Product, ProductVariant

        sku = sku or f"SKU-{uuid.uuid4().hex[:8]}"
        with tenant_context(tenant.org.pk):
            product = Product.objects.create(organization=tenant.org, name=f"Producto {sku}")
            return ProductVariant.objects.create(
                organization=tenant.org,
                product=product,
                sku=sku,
                size="40",
                color="Black",
                price=price,
            )

    return _make


@pytest.fixture
def make_stocked_variant(make_variant):
    """A variant with a known opening balance, ready to be sold."""

    def _make(tenant, quantity: int = 10, price="119000.00", cost="50000.00", sku=None):
        from decimal import Decimal

        from apps.inventory.models import MovementType
        from apps.inventory.services import InventoryService, MovementLine

        variant = make_variant(tenant, sku=sku, price=price)
        with tenant_context(tenant.org.pk):
            InventoryService.apply_movements(
                organization=tenant.org,
                location=tenant.location,
                lines=[
                    MovementLine(
                        variant_id=str(variant.pk), quantity=quantity, unit_cost=Decimal(cost)
                    )
                ],
                movement_type=MovementType.PURCHASE,
            )
            InventoryService.update_average_cost(
                variant=variant, incoming_quantity=quantity, unit_cost=Decimal(cost)
            )
        variant.refresh_from_db()
        return variant

    return _make


@pytest.fixture
def sell():
    """POST a sale with a fresh idempotency key. Returns the DRF response.

    Injects a customer when the caller does not supply one, because every sale
    must be tied to a customer. Pass ``omit_customer=True`` to exercise the
    validation error.
    """

    def _sell(client, lines, payments=None, key=None, omit_customer=False, **extra):
        from decimal import Decimal

        from apps.catalog.models import ProductVariant
        from apps.core.context import tenant_context, unscoped
        from apps.customers.models import Customer

        body = {"lines": lines, **extra}
        if payments is None:
            # Pay exactly the shelf price unless the test says otherwise.
            with unscoped():
                prices = {
                    str(v.pk): v.price
                    for v in ProductVariant.objects.filter(
                        pk__in=[line["variant"] for line in lines]
                    )
                }
            total = sum(
                prices[str(line["variant"])] * line["quantity"]
                - Decimal(str(line.get("discount_amount", 0)))
                for line in lines
            )
            body["payments"] = [{"method": "CASH", "amount": str(total)}]
        else:
            body["payments"] = payments

        if not omit_customer and "customer" not in body:
            with unscoped():
                variant = ProductVariant.objects.get(pk=lines[0]["variant"])
                org = variant.organization
            with tenant_context(org.pk):
                customer = Customer.objects.filter(organization=org, name="Cliente Test").first()
                if customer is None:
                    customer = Customer.objects.create(organization=org, name="Cliente Test")
            body["customer"] = str(customer.pk)

        return client.post(
            "/api/v1/sales/",
            body,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
        )

    return _sell


@pytest.fixture
def open_register():
    """Create a register and open a shift on it."""

    def _open(tenant, user=None, opening_amount="100000.00", code="CAJA1"):
        from apps.cash.models import CashRegister
        from apps.cash.services import CashService

        # A shift belongs to the person, not to their membership; accept either
        # so callers can pass whatever fixture they already have at hand.
        user = getattr(user, "user", user) or tenant.owner

        with tenant_context(tenant.org.pk):
            register = CashRegister.objects.create(
                organization=tenant.org, location=tenant.location, name="Caja 1", code=code
            )
            session = CashService.open_session(
                organization=tenant.org,
                register=register,
                user=user,
                opening_amount=opening_amount,
            )
        return register, session

    return _open


@pytest.fixture
def device():
    """A registered terminal allowed to push offline operations."""

    def _device(tenant, identifier="TILL-01", cash_register=None):
        from apps.synchronization.models import Device

        with tenant_context(tenant.org.pk):
            return Device.objects.create(
                organization=tenant.org,
                location=tenant.location,
                identifier=identifier,
                name="Caja móvil",
                platform="android",
                cash_register=cash_register,
            )

    return _device


@pytest.fixture
def push():
    """POST a batch of offline operations."""

    def _push(client, device, operations):
        return client.post(
            "/api/v1/sync/operations/",
            {"device": str(device.pk), "operations": operations},
            format="json",
        )

    return _push
