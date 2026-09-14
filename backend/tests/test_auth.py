"""Entrar: una identidad, varios negocios, y un token que solo abre uno."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.accounts import services
from apps.accounts.models import Membership
from apps.core.context import tenant_context
from apps.synchronization.models import Device
from apps.synchronization.selectors import issue_device_token

pytestmark = pytest.mark.django_db

User = get_user_model()
LOGIN = "/api/v1/auth/login/"
SELECT = "/api/v1/auth/select-organization/"


# -- Alta ----------------------------------------------------------------


def test_public_signup_is_disabled(client, db):
    response = client.post(
        "/api/v1/auth/register/",
        {
            "email": "iber@example.com",
            "password": "ClaveDePrueba123",
            "organization_name": "Boutique Iber",
        },
        content_type="application/json",
    )

    assert response.status_code == 403
    assert response.json()["code"] == "registration_disabled"


def test_signing_up_is_still_disabled_even_with_existing_email(tenant_a, client, db):
    response = client.post(
        "/api/v1/auth/register/",
        {
            "email": tenant_a.owner.email,
            "password": "ClaveDePrueba123",
            "organization_name": "Otra Boutique",
        },
        content_type="application/json",
    )

    assert response.status_code == 403
    assert response.json()["code"] == "registration_disabled"

# -- Camino de identidad global (SSO) ------------------------------------


def test_one_email_opens_the_list_of_businesses(tenant_a, tenant_b, join, client):
    """El caso que justifica todo el modelo: la misma persona en dos tiendas."""
    join(tenant_b, tenant_a.owner, role=Membership.Role.CASHIER)

    response = client.post(
        LOGIN,
        {"email": tenant_a.owner.email, "password": "ClaveDePrueba123"},
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "identity"
    slugs = {row["organization"]["slug"] for row in body["organizations"]}
    assert slugs == {tenant_a.org.slug, tenant_b.org.slug}
    # Un token de identidad no trae rol ni capacidades: todavía no hay negocio.
    assert "capabilities" not in body


def test_a_single_business_skips_the_picker(tenant_a, client):
    response = client.post(
        LOGIN,
        {"email": tenant_a.owner.email, "password": "ClaveDePrueba123"},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["scope"] == "session"
    assert response.json()["organization"]["slug"] == tenant_a.org.slug


def test_an_identity_token_cannot_operate_any_business(tenant_a, identity_client_for):
    """Sirve para elegir, no para trabajar."""
    client = identity_client_for(tenant_a.owner)

    assert client.get("/api/v1/products/").status_code == 403
    # Pero sí para ver entre qué negocios elegir.
    assert client.get("/api/v1/auth/organizations/").status_code == 200


def test_selecting_a_business_mints_a_session_for_it(tenant_a, tenant_b, join, identity_client_for):
    join(tenant_b, tenant_a.owner, role=Membership.Role.CASHIER)
    client = identity_client_for(tenant_a.owner)

    response = client.post(SELECT, {"organization": tenant_b.org.slug}, format="json")

    assert response.status_code == 200
    body = response.json()
    assert body["organization"]["slug"] == tenant_b.org.slug
    # El rol es el de *ese* negocio, no el de la persona.
    assert body["role"] == "CASHIER"


def test_switching_business_does_not_ask_for_the_password_again(
    tenant_a, tenant_b, join, client_for
):
    join(tenant_b, tenant_a.owner, role=Membership.Role.MANAGER)
    client = client_for(tenant_a.owner, tenant_a.org)

    response = client.post(SELECT, {"organization": tenant_b.org.slug}, format="json")

    assert response.status_code == 200
    assert response.json()["role"] == "MANAGER"


def test_selecting_a_business_you_do_not_belong_to_is_a_404(tenant_a, tenant_b, client_for):
    """404 y no 403: no debe servir para confirmar que la tienda existe."""
    response = client_for(tenant_a.owner, tenant_a.org).post(
        SELECT, {"organization": tenant_b.org.slug}, format="json"
    )

    assert response.status_code == 404


def test_opening_a_second_business_from_the_client_is_disabled(tenant_a, client_for):
    client = client_for(tenant_a.owner, tenant_a.org)

    response = client.post("/api/v1/auth/organizations/new/", {"name": "Sucursal Norte"}, format="json")

    assert response.status_code == 403
    assert response.json()["code"] == "organization_creation_disabled"
    assert Membership.objects.filter(user=tenant_a.owner).count() == 1


# -- Camino de negocio explícito (el mostrador) --------------------------


def test_a_cashier_without_email_signs_in_with_slug_username_and_password(
    tenant_a, make_employee, client
):
    """El alta en el mostrador sigue funcionando igual que antes."""
    membership = make_employee(tenant_a, username="jperez", password="ClaveDePrueba123")
    assert membership.user.email is None

    response = client.post(
        LOGIN,
        {"organization": tenant_a.org.slug, "username": "jperez", "password": "ClaveDePrueba123"},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["organization"]["id"] == str(tenant_a.org.pk)


def test_login_by_username_needs_the_business_slug(tenant_a, make_employee, client):
    make_employee(tenant_a, username="jperez", password="ClaveDePrueba123")
    body = {"username": "jperez", "password": "ClaveDePrueba123"}

    without_slug = client.post(LOGIN, body, content_type="application/json")
    with_slug = client.post(
        LOGIN, {**body, "organization": tenant_a.org.slug}, content_type="application/json"
    )

    assert without_slug.status_code == 401
    assert with_slug.status_code == 200


def test_the_same_username_in_another_business_is_a_different_account(
    tenant_a, tenant_b, make_employee, client
):
    make_employee(tenant_a, username="jperez", password="ClaveDeA123")
    make_employee(tenant_b, username="jperez", password="ClaveDeB123")

    in_a = client.post(
        LOGIN,
        {"organization": tenant_a.org.slug, "username": "jperez", "password": "ClaveDeA123"},
        content_type="application/json",
    )
    crossed = client.post(
        LOGIN,
        {"organization": tenant_b.org.slug, "username": "jperez", "password": "ClaveDeA123"},
        content_type="application/json",
    )

    assert in_a.status_code == 200
    assert in_a.json()["organization"]["id"] == str(tenant_a.org.pk)
    # La contraseña de A no vale nada en B aunque el usuario coincida.
    assert crossed.status_code == 401


def test_an_unknown_slug_and_a_wrong_password_are_indistinguishable(tenant_a, make_employee, client):
    make_employee(tenant_a, username="jperez", password="ClaveDePrueba123")

    unknown_shop = client.post(
        LOGIN,
        {"organization": "no-existe", "username": "jperez", "password": "ClaveDePrueba123"},
        content_type="application/json",
    )
    wrong_password = client.post(
        LOGIN,
        {"organization": tenant_a.org.slug, "username": "jperez", "password": "OtraClave123"},
        content_type="application/json",
    )

    assert unknown_shop.status_code == wrong_password.status_code == 401
    assert unknown_shop.json() == wrong_password.json()


def test_an_unknown_email_and_a_wrong_password_are_indistinguishable(tenant_a, client):
    unknown = client.post(
        LOGIN,
        {"email": "nadie@example.com", "password": "ClaveDePrueba123"},
        content_type="application/json",
    )
    wrong = client.post(
        LOGIN,
        {"email": tenant_a.owner.email, "password": "ClaveEquivocada123"},
        content_type="application/json",
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_a_registered_terminal_supplies_the_business(tenant_a, make_employee, client):
    """En una caja el cajero escribe usuario y contraseña, nada más."""
    membership = make_employee(tenant_a, username="jperez", password="ClaveDePrueba123")
    with tenant_context(tenant_a.org.pk):
        device = Device.objects.create(
            organization=tenant_a.org,
            location=tenant_a.location,
            identifier="TILL-AUTH",
            name="Caja 1",
        )
        token = issue_device_token(device)

    response = client.post(
        LOGIN,
        {"username": "jperez", "password": "ClaveDePrueba123"},
        content_type="application/json",
        headers={"x-device-token": token},
    )

    assert response.status_code == 200
    assert response.json()["membership"] == str(membership.pk)


def test_a_tampered_device_token_resolves_nothing(tenant_a, make_employee, client):
    make_employee(tenant_a, username="jperez", password="ClaveDePrueba123")
    with tenant_context(tenant_a.org.pk):
        device = Device.objects.create(
            organization=tenant_a.org,
            location=tenant_a.location,
            identifier="TILL-BAD",
            name="Caja 2",
        )
        token = issue_device_token(device)

    response = client.post(
        LOGIN,
        {"username": "jperez", "password": "ClaveDePrueba123"},
        content_type="application/json",
        headers={"x-device-token": f"{token}tampered"},
    )

    assert response.status_code == 401


# -- Bloqueo -------------------------------------------------------------


def test_five_bad_passwords_lock_the_membership(tenant_a, make_employee):
    membership = make_employee(tenant_a, username="jperez", password="ClaveDePrueba123")

    for _ in range(services.MAX_FAILED_ATTEMPTS):
        assert not services.verify_credentials(membership, password="mala")

    membership.refresh_from_db()
    assert membership.status == Membership.Status.LOCKED
    # Ni siquiera la contraseña correcta sirve mientras dura el bloqueo.
    assert not services.verify_credentials(membership, password="ClaveDePrueba123")


def test_a_lockout_in_one_business_does_not_close_the_other(tenant_a, tenant_b, join, make_identity):
    """Los contadores son por membresía: es una caja la que se cierra, no la persona."""
    person = make_identity(password="ClaveDePrueba123")
    in_a = join(tenant_a, person)
    in_b = join(tenant_b, person)

    for _ in range(services.MAX_FAILED_ATTEMPTS):
        services.verify_credentials(in_a, password="mala")

    in_a.refresh_from_db()
    in_b.refresh_from_db()
    assert in_a.status == Membership.Status.LOCKED
    assert in_b.status == Membership.Status.ACTIVE
    assert services.verify_credentials(in_b, password="ClaveDePrueba123")


def test_a_good_password_clears_the_failure_count(tenant_a, make_employee):
    membership = make_employee(tenant_a, username="jperez", password="ClaveDePrueba123")

    assert not services.verify_credentials(membership, password="mala")
    assert services.verify_credentials(membership, password="ClaveDePrueba123")

    membership.refresh_from_db()
    assert membership.failed_attempts == 0


# -- El token de sesión --------------------------------------------------


def test_suspending_a_membership_invalidates_the_token_it_already_had(
    tenant_a, make_employee, client_for
):
    """Sin esperar a que caduque: la membresía se revalida en cada petición."""
    membership = make_employee(tenant_a, username="jperez")
    client = client_for(membership)
    assert client.get("/api/v1/products/").status_code == 200

    membership.status = Membership.Status.SUSPENDED
    membership.save(update_fields=["status"])

    assert client.get("/api/v1/products/").status_code == 401


def test_deactivating_the_business_invalidates_its_sessions(tenant_a, client_for):
    client = client_for(tenant_a.owner, tenant_a.org)
    assert client.get("/api/v1/products/").status_code == 200

    tenant_a.org.is_active = False
    tenant_a.org.save(update_fields=["is_active"])

    assert client.get("/api/v1/products/").status_code == 401


def test_a_refreshed_access_token_keeps_the_business(tenant_a, client):
    from rest_framework.test import APIClient

    from apps.accounts.tokens import issue_session_tokens

    tokens = issue_session_tokens(tenant_a.membership)
    refreshed = client.post(
        "/api/v1/auth/refresh/", {"refresh": tokens["refresh"]}, content_type="application/json"
    )

    assert refreshed.status_code == 200
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refreshed.json()['access']}")
    # El claim de organización sobrevive al refresco, así que el token sigue
    # abriendo el negocio - y solo ese.
    assert client.get("/api/v1/auth/me/").json()["organization"]["slug"] == tenant_a.org.slug


def test_a_token_pointing_at_a_business_you_left_stops_working(
    tenant_a, tenant_b, join, client_for
):
    membership = join(tenant_b, tenant_a.owner)
    client = client_for(membership)
    assert client.get("/api/v1/products/").status_code == 200

    membership.delete()

    assert client.get("/api/v1/products/").status_code == 401


# -- /auth/me/ -----------------------------------------------------------


def test_me_reports_the_current_business_and_its_capabilities(tenant_a, client_for):
    data = client_for(tenant_a.owner, tenant_a.org).get("/api/v1/auth/me/").json()

    assert data["scope"] == "session"
    assert data["role"] == "OWNER"
    assert data["organization"]["slug"] == tenant_a.org.slug
    assert "users.manage" in data["capabilities"]


def test_me_with_an_identity_token_lists_the_businesses_instead(
    tenant_a, tenant_b, join, identity_client_for
):
    join(tenant_b, tenant_a.owner)
    data = identity_client_for(tenant_a.owner).get("/api/v1/auth/me/").json()

    assert data["scope"] == "identity"
    assert len(data["organizations"]) == 2
    assert "capabilities" not in data


def test_a_person_edits_their_own_profile(tenant_a, client_for):
    response = client_for(tenant_a.owner, tenant_a.org).patch(
        "/api/v1/auth/me/", {"first_name": "Iber"}, format="json"
    )

    assert response.status_code == 200
    tenant_a.owner.refresh_from_db()
    assert tenant_a.owner.first_name == "Iber"


def test_a_forged_organization_claim_opens_nothing(tenant_a, tenant_b):
    """El claim se firma, pero además se revalida: falsificarlo no basta."""
    from rest_framework.test import APIClient
    from rest_framework_simplejwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(tenant_a.owner)
    refresh["scope"] = "session"
    # Un negocio en el que esta persona no tiene ninguna membresía.
    refresh["organization"] = str(tenant_b.org.pk)

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    assert client.get("/api/v1/products/").status_code == 401
