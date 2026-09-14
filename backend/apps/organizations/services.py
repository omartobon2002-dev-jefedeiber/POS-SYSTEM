"""Aprovisionamiento de negocios."""
from __future__ import annotations

import re

from django.db import transaction

from apps.core.audit import record_audit
from apps.core.context import tenant_context

from .models import Location, Organization

# Con las que arranca cualquier tienda. Son solo un punto de partida: el dueño
# las renombra, desactiva o amplía desde /expense-categories/.
DEFAULT_EXPENSE_CATEGORIES = (
    "Arriendo",
    "Nómina",
    "Servicios públicos",
    "Transporte y domicilios",
    "Aseo y papelería",
    "Publicidad",
    "Mantenimiento",
    "Impuestos y comisiones bancarias",
    "Otros",
)


def derive_username(*, organization, user, preferred: str | None = None) -> str:
    """Un nombre de usuario válido y libre dentro de este negocio.

    El dueño nunca lo escribe al registrarse: se deriva de su correo. Es solo
    la identidad local con la que podrá entrar desde una caja, y puede
    cambiarla después.
    """
    from apps.accounts.models import Membership

    base = preferred or (user.email or "").split("@")[0]
    base = re.sub(r"[^a-z0-9._-]", "", base.strip().lower()).lstrip("._-")
    if len(base) < 3:
        base = f"{base}usuario"[:12] if base else "propietario"

    candidate = base
    suffix = 2
    while Membership.objects.filter(organization=organization, username=candidate).exists():
        candidate = f"{base}{suffix}"
        suffix += 1
    return candidate


@transaction.atomic
def provision_organization(
    *,
    user,
    name: str,
    legal_name: str = "",
    tax_id: str = "",
    username: str | None = None,
    plan=None,
    plan_code: str | None = None,
    months: int = 1,
    trial_days: int | None = None,
    use_trial: bool = True,
):
    """Crea un negocio con todo lo necesario para usarlo de inmediato.

    Un tenant nunca queda a medio construir: la membresía de dueño, una sede
    por defecto, una caja y una suscripción se crean en la misma transacción
    que la organización.

    Por defecto (`use_trial=True`) abre un trial de 14 días — legado del
    self-serve. Los operadores de plataforma pasan `use_trial=False` (o
    `trial_days`) para provisionar con periodo ACTIVE pagado.
    """
    from apps.accounts.models import Membership

    organization = Organization.objects.create(name=name, legal_name=legal_name, tax_id=tax_id)

    with tenant_context(organization.pk):
        location = Location.objects.create(
            organization=organization,
            name="Principal",
            code="PRINCIPAL",
            is_default=True,
        )

        from apps.cash.models import CashRegister

        CashRegister.objects.create(
            organization=organization,
            location=location,
            name="Principal",
            code="PRINCIPAL",
        )

        membership = Membership.objects.create(
            user=user,
            organization=organization,
            username=derive_username(organization=organization, user=user, preferred=username),
            role=Membership.Role.OWNER,
            status=Membership.Status.ACTIVE,
            default_location=location,
        )

        from apps.expenses.models import ExpenseCategory

        ExpenseCategory.objects.bulk_create(
            [
                ExpenseCategory(organization=organization, name=cat_name)
                for cat_name in DEFAULT_EXPENSE_CATEGORIES
            ]
        )

        from apps.subscriptions.services import start_active_subscription, start_trial_subscription

        if use_trial and trial_days is None:
            start_trial_subscription(
                organization=organization,
                plan_code=plan_code or "BASIC",
            )
        else:
            start_active_subscription(
                organization=organization,
                plan=plan,
                plan_code=plan_code or "BASIC",
                months=months,
                trial_days=trial_days,
            )

        record_audit(
            organization=organization,
            action="organization.created",
            actor=user,
            obj=organization,
            metadata={"name": name},
        )

    return membership
