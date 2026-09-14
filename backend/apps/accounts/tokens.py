"""Dos clases de token, y la diferencia importa.

El *token de identidad* dice quién eres y nada más: sirve para listar tus
negocios, elegir uno y abrir uno nuevo. No abre ningún endpoint de negocio.

El *token de sesión* añade el claim `organization`, que es lo que establece el
tenant en cada petición. Ese claim nunca se cree por sí solo: se revalida
contra una membresía ACTIVA en cada request (ver apps.core.authentication), de
modo que suspender a un empleado surte efecto de inmediato sin esperar a que
el token caduque.

El *token de plataforma* identifica a un operador staff sin membresías; no
abre endpoints de negocio.

simplejwt copia los claims personalizados del refresh al access que emite
(`RefreshToken.access_token` solo excluye exp/jti/token_type), así que refrescar
conserva la organización sin código adicional.
"""
from __future__ import annotations

from rest_framework_simplejwt.tokens import RefreshToken

SCOPE_CLAIM = "scope"
ORGANIZATION_CLAIM = "organization"
MEMBERSHIP_CLAIM = "membership"

IDENTITY_SCOPE = "identity"
SESSION_SCOPE = "session"
PLATFORM_SCOPE = "platform"


def issue_identity_tokens(user) -> dict[str, str]:
    """Un par access/refresh que solo identifica a la persona."""
    refresh = RefreshToken.for_user(user)
    refresh[SCOPE_CLAIM] = IDENTITY_SCOPE
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


def issue_session_tokens(membership) -> dict[str, str]:
    """Un par access/refresh atado a un negocio concreto.

    `role` viaja solo como información para el cliente; la autorización nunca
    lo lee, siempre releé la membresía.
    """
    refresh = RefreshToken.for_user(membership.user)
    refresh[SCOPE_CLAIM] = SESSION_SCOPE
    refresh[ORGANIZATION_CLAIM] = str(membership.organization_id)
    refresh[MEMBERSHIP_CLAIM] = str(membership.pk)
    refresh["role"] = membership.role
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


def issue_platform_tokens(user) -> dict[str, str]:
    """Access/refresh for a platform operator (no organization claim)."""
    refresh = RefreshToken.for_user(user)
    refresh[SCOPE_CLAIM] = PLATFORM_SCOPE
    return {"refresh": str(refresh), "access": str(refresh.access_token)}
