from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenRefreshView

from apps.core import capabilities as caps
from apps.core.audit import record_audit
from apps.core.context import tenant_context
from apps.core.exceptions import InvalidOperation, OrganizationSuspended, SharedIdentity, SubscriptionInactive
from apps.core.permissions import HasCapability, HasOrganization, SubscriptionAllowsWrites
from apps.organizations.models import Location, Organization
from apps.organizations.serializers import OrganizationSerializer
from apps.subscriptions.limits import enforce_limit
from apps.subscriptions.services import expire_if_needed
from apps.synchronization.selectors import DEVICE_TOKEN_HEADER, resolve_device

from . import services
from .invitations import issue_invitation_token, resolve_invitation, send_invitation_email
from .models import Invitation, Membership
from .serializers import (
    EmployeeCreateSerializer,
    EmployeeProfileUpdateSerializer,
    EmployeeSerializer,
    EmployeeUpdateSerializer,
    InvitationAcceptSerializer,
    InvitationCreateSerializer,
    InvitationPreviewSerializer,
    InvitationSerializer,
    LoginSerializer,
    MembershipBriefSerializer,
    OrganizationCreateSerializer,
    ProfileUpdateSerializer,
    RegistrationSerializer,
    SelectOrganizationSerializer,
    UserSerializer,
)
from .tokens import issue_identity_tokens, issue_session_tokens


def _ensure_tenant_access(organization) -> None:
    """Raise if the business is suspended or the subscription does not grant access."""
    if not organization.is_active:
        raise OrganizationSuspended(
            f"The organization {organization.name} has been suspended.",
        )
    from apps.subscriptions.models import Subscription

    subscription = Subscription.all_objects.filter(organization=organization).first()
    if subscription is not None:
        expire_if_needed(subscription)
        subscription.refresh_from_db()
        if not subscription.grants_access:
            raise SubscriptionInactive(
                f"The subscription for {organization.name} is {subscription.status.lower()}.",
                status=subscription.status,
            )

User = get_user_model()

# Un solo mensaje para un slug equivocado, un correo desconocido, un usuario que
# no existe y un secreto malo: el endpoint no debe revelar qué tiendas existen
# ni quién trabaja en ellas.
INVALID_CREDENTIALS = {"detail": "Invalid credentials.", "code": "invalid_credentials"}
INVALID_INVITATION = {"detail": "Esa invitación ya no es válida.", "code": "invalid_invitation"}


def session_payload(membership) -> dict:
    """Todo lo que el cliente necesita para trabajar dentro de un negocio."""
    return {
        **issue_session_tokens(membership),
        "scope": "session",
        "user": UserSerializer(membership.user).data,
        "organization": OrganizationSerializer(membership.organization).data,
        "membership": str(membership.pk),
        "role": membership.role,
        "capabilities": sorted(membership.capabilities),
        "default_location": str(membership.default_location_id)
        if membership.default_location_id
        else None,
    }


def identity_payload(user, memberships) -> dict:
    """Quién eres y entre qué negocios puedes elegir. No abre ninguno."""
    return {
        **issue_identity_tokens(user),
        "scope": "identity",
        "user": UserSerializer(user).data,
        "organizations": MembershipBriefSerializer(memberships, many=True).data,
    }


class RegisterView(APIView):
    """Public self-serve signup is disabled — businesses are provisioned by platform operators."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_scope = "register"
    serializer_class = RegistrationSerializer

    @extend_schema(request=RegistrationSerializer, responses={403: None})
    def post(self, request):
        return Response(
            {
                "detail": "El registro público está deshabilitado. Contacta al administrador de la plataforma.",
                "code": "registration_disabled",
            },
            status=status.HTTP_403_FORBIDDEN,
        )


class LoginView(APIView):
    """Iniciar sesión, por cualquiera de los dos caminos.

    El orden importa: si la petición dice de qué negocio se trata (terminal
    registrada o slug), se resuelve ahí mismo y se devuelve una sesión. Solo
    cuando no lo dice se cae al camino de identidad global, que responde con la
    lista de negocios para que el cliente elija.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_scope = "auth"
    serializer_class = LoginSerializer

    @extend_schema(request=LoginSerializer, responses={200: None})
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        organization = self._resolve_organization(request, data.get("organization"))
        if organization is not None or data.get("username"):
            return self._login_into_organization(organization, data)
        return self._login_with_identity(data)

    # -- Caminos 1 y 2: el negocio ya se conoce --------------------------
    def _login_into_organization(self, organization, data):
        if organization is None:
            return Response(INVALID_CREDENTIALS, status=status.HTTP_401_UNAUTHORIZED)
        membership = services.find_membership(
            organization=organization, username=data.get("username", "")
        )
        if not services.verify_credentials(membership, password=data.get("password") or None):
            return Response(INVALID_CREDENTIALS, status=status.HTTP_401_UNAUTHORIZED)

        _ensure_tenant_access(membership.organization)
        services.touch(membership)
        return Response(session_payload(membership))

    # -- Camino 3: identidad global (SSO) --------------------------------
    def _login_with_identity(self, data):
        user = services.find_identity(email=data.get("email", ""))
        if not services.verify_identity(user, data.get("password") or None):
            return Response(INVALID_CREDENTIALS, status=status.HTTP_401_UNAUTHORIZED)

        memberships = list(services.active_memberships(user))
        if not memberships:
            # Credentials are valid but every business is suspended (or none).
            suspended = Membership.objects.filter(
                user=user,
                status=Membership.Status.ACTIVE,
                organization__is_active=False,
            ).select_related("organization").first()
            if suspended is not None:
                raise OrganizationSuspended(
                    f"The organization {suspended.organization.name} has been suspended.",
                )
            return Response(identity_payload(user, memberships))

        # Con un solo negocio no hay nada que elegir: ahorrarle al cliente un
        # viaje redundante es el caso mayoritario, no una optimización.
        if len(memberships) == 1:
            _ensure_tenant_access(memberships[0].organization)
            services.touch(memberships[0])
            return Response(session_payload(memberships[0]))
        return Response(identity_payload(user, memberships))

    @staticmethod
    def _resolve_organization(request, slug: str | None):
        device = resolve_device(request.headers.get(DEVICE_TOKEN_HEADER))
        if device is not None:
            return device.organization
        if not slug:
            return None
        return Organization.objects.filter(slug=slug, is_active=True).first()


class OrganizationChoicesView(APIView):
    """Los negocios en los que puede trabajar quien pregunta.

    Vale tanto con un token de identidad como con uno de sesión: cambiar de
    negocio no debe obligar a volver a escribir la contraseña.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: MembershipBriefSerializer(many=True)})
    def get(self, request):
        memberships = services.active_memberships(request.user)
        return Response(MembershipBriefSerializer(memberships, many=True).data)


class SelectOrganizationView(APIView):
    """Elegir negocio, o cambiar de negocio. Es la misma operación.

    La membresía se revalida aquí y otra vez en cada petición posterior, así que
    este endpoint no otorga nada que la autenticación no vuelva a comprobar.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "auth"
    serializer_class = SelectOrganizationSerializer

    @extend_schema(request=SelectOrganizationSerializer, responses={200: None})
    def post(self, request):
        serializer = SelectOrganizationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        wanted = serializer.validated_data["organization"].strip()

        membership = next(
            (
                m
                for m in services.active_memberships(request.user)
                if str(m.organization_id) == wanted or m.organization.slug == wanted
            ),
            None,
        )
        # 404 y no 403: quien no es miembro no debe poder confirmar que el
        # negocio existe.
        if membership is None:
            return Response(
                {"detail": "No encontrado.", "code": "not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        _ensure_tenant_access(membership.organization)
        services.touch(membership)
        return Response(session_payload(membership))


class MeView(APIView):
    """La sesión actual, releída de la base de datos."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: None})
    def get(self, request):
        membership = getattr(request, "membership", None)
        if membership is None:
            # Token de identidad: aún no se ha elegido negocio.
            return Response(identity_payload_without_tokens(request.user))
        return Response(
            {
                "scope": "session",
                "user": UserSerializer(membership.user).data,
                "organization": OrganizationSerializer(membership.organization).data,
                "membership": str(membership.pk),
                "role": membership.role,
                "capabilities": sorted(membership.capabilities),
                "default_location": str(membership.default_location_id)
                if membership.default_location_id
                else None,
            }
        )

    @extend_schema(request=ProfileUpdateSerializer, responses={200: UserSerializer})
    def patch(self, request):
        serializer = ProfileUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserSerializer(request.user).data)


def identity_payload_without_tokens(user) -> dict:
    """`/auth/me/` con un token de identidad: el mismo cuerpo, sin emitir tokens nuevos."""
    return {
        "scope": "identity",
        "user": UserSerializer(user).data,
        "organizations": MembershipBriefSerializer(
            services.active_memberships(user), many=True
        ).data,
    }


class CreateOrganizationView(APIView):
    """Self-serve creation of additional businesses is disabled."""

    permission_classes = [IsAuthenticated]
    serializer_class = OrganizationCreateSerializer

    @extend_schema(request=OrganizationCreateSerializer, responses={403: None})
    def post(self, request):
        return Response(
            {
                "detail": "Solo un operador de plataforma puede crear negocios.",
                "code": "organization_creation_disabled",
            },
            status=status.HTTP_403_FORBIDDEN,
        )


class RefreshView(TokenRefreshView):
    """Refresco estándar.

    simplejwt copia los claims personalizados del refresh al access que emite,
    así que la organización sobrevive al refresco sin código propio - y sigue
    revalidándose contra la membresía en cada petición.
    """

    throttle_scope = "auth"


class EmployeeViewSet(viewsets.ModelViewSet):
    """`Ajustes -> Empleados`. Opera sobre membresías, no sobre personas.

    El id de la ruta es el de la membresía: la misma persona puede estar en dos
    negocios y cada uno solo administra su lado de la relación.

    A propósito no es un TenantModelViewSet: ese mixin lee
    `model._default_manager`, y Membership no puede llevar un TenantManager
    porque se consulta antes de que exista contexto (en el login) y de forma
    cruzada (los negocios de una persona). El filtro va explícito abajo.
    """

    permission_classes = [HasOrganization, HasCapability, SubscriptionAllowsWrites]
    read_capability = caps.ORGANIZATION_READ
    write_capability = caps.USERS_MANAGE
    serializer_class = EmployeeSerializer
    filterset_fields = ["role", "status"]
    search_fields = ["username", "user__first_name", "user__last_name", "user__email"]

    queryset = Membership.objects.none()  # solo para el esquema; ver get_queryset

    def get_queryset(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            return Membership.objects.none()
        return Membership.objects.filter(organization=organization).select_related(
            "user", "default_location"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return EmployeeCreateSerializer
        if self.action in ("update", "partial_update"):
            return EmployeeUpdateSerializer
        return EmployeeSerializer

    @extend_schema(request=EmployeeCreateSerializer, responses={201: EmployeeSerializer})
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = EmployeeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        organization = request.organization

        enforce_limit(
            organization=organization,
            resource="users",
            current_count=count_seats(organization),
        )

        if self.get_queryset().filter(username=data["username"]).exists():
            return Response(
                {
                    "detail": "Alguien en este negocio ya usa ese usuario.",
                    "code": "username_taken",
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Si el correo ya tiene cuenta se reutiliza la persona: eso es
        # exactamente lo que permite trabajar en dos negocios con una identidad.
        user = services.find_identity(email=data["email"])
        if user is not None:
            if self.get_queryset().filter(user=user).exists():
                return Response(
                    {
                        "detail": "Esa persona ya forma parte de este negocio.",
                        "code": "already_member",
                    },
                    status=status.HTTP_409_CONFLICT,
                )
        else:
            user = User.objects.create_user(
                email=data["email"],
                password=data["password"],
                first_name=data["first_name"],
                last_name=data.get("last_name", ""),
                phone=data.get("phone", ""),
            )

        location_id = data.get("default_location")
        membership = Membership.objects.create(
            user=user,
            organization=organization,
            username=data["username"],
            role=data["role"],
            status=Membership.Status.ACTIVE,
            default_location=Location.objects.filter(pk=location_id).first()
            if location_id
            else None,
        )

        record_audit(
            organization=organization,
            action="user.created",
            actor=request.user,
            obj=membership,
            metadata={"username": membership.username, "role": membership.role},
        )
        return Response(EmployeeSerializer(membership).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        instance = serializer.instance
        previous_role = instance.role
        new_role = serializer.validated_data.get("role", previous_role)
        new_status = serializer.validated_data.get("status", instance.status)

        # Se comprueba antes de guardar, no después: un dueño degradado en un
        # negocio sin otro dueño ya estaría persistido cuando lo notáramos.
        if new_role != Membership.Role.OWNER or new_status != Membership.Status.ACTIVE:
            self._guard_last_owner(instance)

        membership = serializer.save()
        self._update_profile(membership)

        if previous_role != membership.role:
            record_audit(
                organization=self.request.organization,
                action="user.permission_changed",
                actor=self.request.user,
                obj=membership,
                metadata={"from": previous_role, "to": membership.role},
            )

    def _update_profile(self, membership):
        """Nombre, correo y teléfono son de la persona, no del negocio.

        Solo se dejan editar cuando esa persona no trabaja en ningún otro sitio;
        si no, una tienda podría renombrar a alguien dentro de otra.
        """
        fields = {
            key: value
            for key, value in self.request.data.items()
            if key in ("first_name", "last_name", "phone", "email")
        }
        if not fields:
            return

        shared = Membership.objects.filter(user=membership.user).exclude(pk=membership.pk).exists()
        if shared:
            raise SharedIdentity(
                "Esa persona también trabaja en otro negocio: solo ella puede cambiar sus datos "
                "personales."
            )

        serializer = EmployeeProfileUpdateSerializer(membership.user, data=fields, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

    def perform_destroy(self, instance):
        self._guard_last_owner(instance)
        # Nunca se borra: el historial de ventas, cajas y auditoría apunta aquí.
        instance.status = Membership.Status.SUSPENDED
        instance.save(update_fields=["status", "updated_at"])
        record_audit(
            organization=self.request.organization,
            action="user.deactivated",
            actor=self.request.user,
            obj=instance,
            metadata={"username": instance.username},
        )

    @extend_schema(request=None, responses={200: EmployeeSerializer})
    @action(detail=True, methods=["post"])
    def unlock(self, request, pk=None):
        """Levanta el bloqueo por intentos fallidos.

        Es la única recuperación para el personal de mostrador: sin correo y
        sin enlace, el dueño la levanta en el momento.
        """
        membership = self.get_object()
        services.unlock(membership)

        record_audit(
            organization=request.organization,
            action="user.unlocked",
            actor=request.user,
            obj=membership,
            metadata={"username": membership.username},
        )
        return Response(EmployeeSerializer(membership).data)

    def _guard_last_owner(self, membership):
        """Un negocio sin dueño no se puede volver a administrar nunca."""
        if membership.role != Membership.Role.OWNER:
            return
        remaining = (
            self.get_queryset()
            .filter(role=Membership.Role.OWNER, status=Membership.Status.ACTIVE)
            .exclude(pk=membership.pk)
            .count()
        )
        if remaining == 0:
            raise InvalidOperation("Un negocio debe conservar al menos un dueño activo.")


def count_seats(organization) -> int:
    """Los puestos que ocupa un negocio en su plan.

    Una invitación pendiente ya cuenta: si no, invitar a diez personas
    permitiría saltarse el límite hasta que aceptaran.
    """
    members = Membership.objects.filter(organization=organization).exclude(
        status=Membership.Status.SUSPENDED
    )
    pending = Invitation.objects.filter(
        organization=organization,
        status=Invitation.Status.PENDING,
        expires_at__gt=timezone.now(),
    )
    return members.count() + pending.count()


class InvitationViewSet(viewsets.ModelViewSet):
    """Invitar por correo a alguien que quizá ya tenga cuenta en otro negocio."""

    permission_classes = [HasOrganization, HasCapability, SubscriptionAllowsWrites]
    read_capability = caps.ORGANIZATION_READ
    write_capability = caps.USERS_MANAGE
    serializer_class = InvitationSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]
    filterset_fields = ["status", "role"]

    queryset = Invitation.objects.none()  # solo para el esquema

    def get_queryset(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            return Invitation.objects.none()
        return Invitation.objects.filter(organization=organization).select_related("invited_by")

    @extend_schema(request=InvitationCreateSerializer, responses={201: InvitationSerializer})
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = InvitationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        organization = request.organization

        enforce_limit(
            organization=organization,
            resource="users",
            current_count=count_seats(organization),
        )

        existing = services.find_identity(email=data["email"])
        if existing is not None and Membership.objects.filter(
            organization=organization, user=existing
        ).exists():
            return Response(
                {"detail": "Esa persona ya forma parte de este negocio.", "code": "already_member"},
                status=status.HTTP_409_CONFLICT,
            )

        # Una invitación viva por correo y negocio: reemplazar la anterior es
        # lo que espera quien vuelve a pulsar "invitar".
        self.get_queryset().filter(
            email=data["email"], status=Invitation.Status.PENDING
        ).update(status=Invitation.Status.REVOKED)

        location_id = data.get("default_location")
        invitation = Invitation.objects.create(
            organization=organization,
            email=data["email"],
            role=data["role"],
            default_location=Location.objects.filter(pk=location_id).first()
            if location_id
            else None,
            invited_by=request.user,
            expires_at=timezone.now(),  # lo fija issue_invitation_token
        )
        raw_token = issue_invitation_token(invitation)
        send_invitation_email(invitation, raw_token)

        record_audit(
            organization=organization,
            action="invitation.sent",
            actor=request.user,
            obj=invitation,
            metadata={"email": invitation.email, "role": invitation.role},
        )
        return Response(InvitationSerializer(invitation).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        instance.status = Invitation.Status.REVOKED
        instance.save(update_fields=["status", "updated_at"])
        record_audit(
            organization=self.request.organization,
            action="invitation.revoked",
            actor=self.request.user,
            obj=instance,
            metadata={"email": instance.email},
        )

    @extend_schema(request=None, responses={200: InvitationSerializer})
    @action(detail=True, methods=["post"])
    def resend(self, request, pk=None):
        """Emite un token nuevo y reenvía el correo. El anterior deja de servir."""
        invitation = self.get_object()
        if invitation.status != Invitation.Status.PENDING:
            raise InvalidOperation("Esa invitación ya no está pendiente.")

        raw_token = issue_invitation_token(invitation)
        send_invitation_email(invitation, raw_token)
        return Response(InvitationSerializer(invitation).data)


class InvitationPreviewView(APIView):
    """Lo que ve quien abre el enlace, antes de decidir si acepta."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_scope = "auth"

    @extend_schema(responses={200: InvitationPreviewSerializer})
    def get(self, request, token: str):
        invitation = resolve_invitation(token)
        if invitation is None:
            return Response(INVALID_INVITATION, status=status.HTTP_404_NOT_FOUND)

        return Response(
            InvitationPreviewSerializer(
                {
                    "organization": invitation.organization.name,
                    "email": invitation.email,
                    "role": invitation.role,
                    "expires_at": invitation.expires_at,
                    "account_exists": services.find_identity(email=invitation.email) is not None,
                }
            ).data
        )


class InvitationAcceptView(APIView):
    """Liquida el token y crea la membresía.

    Si el correo ya tiene cuenta se reutiliza esa identidad y no se toca su
    contraseña: aceptar una invitación no puede ser una forma de cambiársela.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_scope = "auth"
    serializer_class = InvitationAcceptSerializer

    @extend_schema(request=InvitationAcceptSerializer, responses={201: None})
    @transaction.atomic
    def post(self, request):
        serializer = InvitationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        invitation = resolve_invitation(data["token"])
        if invitation is None:
            return Response(INVALID_INVITATION, status=status.HTTP_400_BAD_REQUEST)

        organization = invitation.organization
        if Membership.objects.filter(
            organization=organization, username=data["username"]
        ).exists():
            return Response(
                {"detail": "Alguien en este negocio ya usa ese usuario.", "code": "username_taken"},
                status=status.HTTP_409_CONFLICT,
            )

        user = services.find_identity(email=invitation.email)
        if user is None:
            if not data.get("password"):
                return Response(
                    {"detail": "Necesitas elegir una contraseña.", "code": "password_required"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user = User.objects.create_user(
                email=invitation.email,
                password=data["password"],
                first_name=data.get("first_name", ""),
                last_name=data.get("last_name", ""),
                phone=data.get("phone", ""),
            )
        elif Membership.objects.filter(organization=organization, user=user).exists():
            return Response(INVALID_INVITATION, status=status.HTTP_400_BAD_REQUEST)

        membership = Membership.objects.create(
            user=user,
            organization=organization,
            username=data["username"],
            role=invitation.role,
            status=Membership.Status.ACTIVE,
            default_location=invitation.default_location,
        )
        invitation.status = Invitation.Status.ACCEPTED
        invitation.accepted_at = timezone.now()
        invitation.membership = membership
        invitation.save(update_fields=["status", "accepted_at", "membership", "updated_at"])

        # Aquí todavía no hay sesión, así que tampoco hay contexto de tenant:
        # se abre explícitamente para poder dejar el rastro de auditoría.
        with tenant_context(organization.pk):
            record_audit(
                organization=organization,
                action="invitation.accepted",
                actor=user,
                obj=membership,
                metadata={"email": invitation.email, "role": membership.role},
            )
        return Response(session_payload(membership), status=status.HTTP_201_CREATED)
