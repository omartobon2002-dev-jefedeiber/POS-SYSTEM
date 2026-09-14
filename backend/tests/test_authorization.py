"""Capabilities are enforced by the server, never by the UI hiding a button.

The subject here is always a membership: what someone may do is a property of
their role inside one business, not of the person.
"""
from __future__ import annotations

import pytest

from apps.accounts.models import Membership
from apps.core import capabilities as caps

pytestmark = pytest.mark.django_db


@pytest.fixture
def cashier(tenant_a, make_employee):
    return make_employee(tenant_a, role=Membership.Role.CASHIER, username="cajero")


def test_cashier_can_read_and_write_the_catalog_but_not_hire(cashier, client_for):
    client = client_for(cashier)

    assert client.get("/api/v1/products/").status_code == 200
    created = client.post(
        "/api/v1/products/",
        {
            "name": "Camiseta",
            "variants": [{"sku": "CAM-001", "price": "45000.00"}],
        },
        format="json",
    )
    assert created.status_code == 201
    assert "average_cost" not in created.data["variants"][0]

    hire = client.post(
        "/api/v1/employees/",
        {"username": "otro", "first_name": "Otro", "role": "CASHIER"},
        format="json",
    )
    assert hire.status_code == 403


def test_cashier_can_adjust_inventory_without_setting_cost(tenant_a, cashier, make_variant, client_for):
    variant = make_variant(tenant_a)
    client = client_for(cashier)

    adjustment = client.post(
        "/api/v1/inventory/adjustments/",
        {
            "lines": [
                {"variant": str(variant.pk), "quantity": 5, "unit_cost": "99999.00"},
            ]
        },
        format="json",
    )
    hire = client.post(
        "/api/v1/employees/",
        {"username": "otro", "first_name": "Otro", "role": "CASHIER"},
        format="json",
    )

    assert adjustment.status_code == 201
    assert "unit_cost" not in adjustment.data[0]
    assert hire.status_code == 403
    variant.refresh_from_db()
    assert variant.average_cost == 0


def test_cashier_lacks_costs_read(cashier):
    assert cashier.has_capability(caps.PRODUCTS_WRITE)
    assert cashier.has_capability(caps.INVENTORY_ADJUST)
    assert not cashier.has_capability(caps.COSTS_READ)
    assert not cashier.has_capability(caps.REPORTS_READ)
    assert not cashier.has_capability(caps.PURCHASES_CREATE)


def test_owner_can_do_what_the_cashier_cannot(tenant_a, make_variant, client_for):
    variant = make_variant(tenant_a)
    client = client_for(tenant_a.owner)

    response = client.post(
        "/api/v1/inventory/adjustments/",
        {"lines": [{"variant": str(variant.pk), "quantity": 5}]},
        format="json",
    )

    assert response.status_code == 201


def test_the_same_username_is_two_unrelated_people(tenant_a, tenant_b, make_employee):
    """`jperez` in one shop has nothing to do with `jperez` in another."""
    in_a = make_employee(
        tenant_a, role=Membership.Role.MANAGER, username="jperez", password="ClaveDeA123"
    )
    in_b = make_employee(
        tenant_b, role=Membership.Role.CASHIER, username="jperez", password="ClaveDeB123"
    )

    assert in_a.pk != in_b.pk
    assert in_a.has_capability(caps.COSTS_READ)
    assert not in_b.has_capability(caps.COSTS_READ)
    # The password of one never opens the other.
    assert not in_b.user.check_password("ClaveDeA123")


def test_the_owner_hires_directly_with_a_username_password_and_email(tenant_a, client_for):
    """No invitation, no acceptance: the account works immediately."""
    client = client_for(tenant_a.owner)

    response = client.post(
        "/api/v1/employees/",
        {
            "username": "jperez",
            "first_name": "Juan",
            "last_name": "Pérez",
            "role": "CASHIER",
            "password": "una-clave-segura",
            "email": "jperez@example.com",
        },
        format="json",
    )

    assert response.status_code == 201
    assert "pin" not in response.data
    assert response.data["status"] == "ACTIVE"

    membership = Membership.objects.get(pk=response.data["id"])
    assert membership.organization_id == tenant_a.org.pk
    assert membership.user.check_password("una-clave-segura")
    # The email is what this person signs in with globally.
    assert membership.user.email == "jperez@example.com"


def test_creating_an_employee_without_an_email_is_refused(tenant_a, client_for):
    client = client_for(tenant_a.owner)

    response = client.post(
        "/api/v1/employees/",
        {
            "username": "jperez",
            "first_name": "Juan",
            "role": "CASHIER",
            "password": "una-clave-segura",
        },
        format="json",
    )

    assert response.status_code == 400
    assert "email" in response.data


def test_an_employee_of_another_business_is_invisible(tenant_a, tenant_b, make_employee, client_for):
    make_employee(tenant_b, username="ajeno")
    client = client_for(tenant_a.owner)

    usernames = [row["username"] for row in client.get("/api/v1/employees/").data["results"]]

    assert "ajeno" not in usernames


def test_a_username_can_be_reused_across_businesses_but_not_within_one(
    tenant_a, tenant_b, make_employee, client_for
):
    make_employee(tenant_b, username="jperez")
    client = client_for(tenant_a.owner)
    body = {
        "username": "jperez",
        "first_name": "Juan",
        "role": "CASHIER",
        "password": "una-clave-segura",
        "email": "jperez.a@example.com",
    }

    # Free in this business even though another shop already uses it.
    assert client.post("/api/v1/employees/", body, format="json").status_code == 201
    # Taken the second time round.
    assert client.post("/api/v1/employees/", body, format="json").status_code == 409


def test_a_deactivated_employee_loses_access_immediately(cashier, client_for):
    """Revocation must not wait for the token to expire."""
    client = client_for(cashier)
    assert client.get("/api/v1/products/").status_code == 200

    cashier.status = Membership.Status.SUSPENDED
    cashier.save(update_fields=["status"])

    assert client.get("/api/v1/products/").status_code == 401


def test_the_last_owner_cannot_be_removed_or_demoted(tenant_a, client_for):
    client = client_for(tenant_a.owner)
    owner_url = f"/api/v1/employees/{tenant_a.membership.pk}/"

    assert client.delete(owner_url).status_code == 400
    assert client.patch(owner_url, {"role": "CASHIER"}, format="json").status_code == 400

    tenant_a.membership.refresh_from_db()
    assert tenant_a.membership.role == Membership.Role.OWNER
    assert tenant_a.membership.status == Membership.Status.ACTIVE


def test_unlocking_a_membership_clears_the_lockout(tenant_a, make_employee, client_for):
    employee = make_employee(tenant_a, status=Membership.Status.LOCKED)
    client = client_for(tenant_a.owner)

    response = client.post(f"/api/v1/employees/{employee.pk}/unlock/")

    assert response.status_code == 200
    employee.refresh_from_db()
    assert employee.status == Membership.Status.ACTIVE
    assert employee.failed_attempts == 0


def test_hiring_by_email_reuses_the_person_who_already_has_an_account(
    tenant_a, tenant_b, make_identity, join, client_for
):
    """Contratar a alguien que ya trabaja en otra tienda no le crea otra cuenta."""
    person = make_identity(email="conocida@example.com")
    join(tenant_b, person)

    response = client_for(tenant_a.owner, tenant_a.org).post(
        "/api/v1/employees/",
        {
            "username": "conocida",
            "first_name": "Ana",
            "role": "CASHIER",
            "email": "conocida@example.com",
            "password": "una-clave-segura",
        },
        format="json",
    )

    assert response.status_code == 201
    assert Membership.objects.filter(user=person).count() == 2
    assert response.data["user_id"] == str(person.pk)


def test_hiring_someone_already_on_the_team_is_refused(tenant_a, make_identity, join, client_for):
    person = make_identity(email="ya@example.com")
    join(tenant_a, person)

    response = client_for(tenant_a.owner, tenant_a.org).post(
        "/api/v1/employees/",
        {
            "username": "otro",
            "first_name": "Ya",
            "role": "CASHIER",
            "email": "ya@example.com",
            "password": "una-clave-segura",
        },
        format="json",
    )

    assert response.status_code == 409
    assert response.data["code"] == "already_member"


def test_a_business_cannot_rename_someone_who_also_works_elsewhere(
    tenant_a, tenant_b, make_identity, join, client_for
):
    """El nombre es de la persona; una tienda no puede cambiárselo dentro de otra."""
    person = make_identity()
    in_a = join(tenant_a, person)
    join(tenant_b, person)

    response = client_for(tenant_a.owner, tenant_a.org).patch(
        f"/api/v1/employees/{in_a.pk}/", {"first_name": "Otro Nombre"}, format="json"
    )

    assert response.status_code == 403
    assert response.data["code"] == "shared_identity"


def test_a_business_can_edit_someone_who_only_works_there(tenant_a, make_employee, client_for):
    employee = make_employee(tenant_a, username="jperez")

    response = client_for(tenant_a.owner, tenant_a.org).patch(
        f"/api/v1/employees/{employee.pk}/", {"first_name": "Juan"}, format="json"
    )

    assert response.status_code == 200
    employee.user.refresh_from_db()
    assert employee.user.first_name == "Juan"


def test_removing_someone_suspends_the_membership_and_keeps_the_person(
    tenant_a, make_employee, client_for
):
    """El historial de ventas y cajas apunta aquí: nunca se borra."""
    employee = make_employee(tenant_a, username="jperez")

    assert client_for(tenant_a.owner, tenant_a.org).delete(
        f"/api/v1/employees/{employee.pk}/"
    ).status_code == 204

    employee.refresh_from_db()
    assert employee.status == Membership.Status.SUSPENDED
    assert employee.user.is_active
