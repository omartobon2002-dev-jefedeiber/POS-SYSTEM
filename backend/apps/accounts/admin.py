from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Count
from django.utils.html import format_html

from .models import Invitation, Membership, User


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    fields = ("organization", "username", "role", "status", "default_location", "last_used_at")
    readonly_fields = ("last_used_at",)
    autocomplete_fields = ("organization", "default_location")
    show_change_link = True


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email", "first_name")
    list_display = (
        "display_identity",
        "full_name",
        "status",
        "is_staff",
        "membership_count",
        "created_at",
    )
    list_filter = ("status", "is_staff", "is_superuser")
    search_fields = (
        "email",
        "first_name",
        "last_name",
        "phone",
        "memberships__username",
    )
    readonly_fields = ("last_login", "created_at", "updated_at")
    inlines = [MembershipInline]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal", {"fields": ("first_name", "last_name", "phone")}),
        ("Estado", {"fields": ("status", "failed_attempts", "locked_until")}),
        ("Plataforma", {"fields": ("is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Fechas", {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "first_name", "last_name", "password1", "password2"),
            },
        ),
    )

    @admin.display(description="Identidad", ordering="email")
    def display_identity(self, obj: User) -> str:
        if obj.email:
            return obj.email
        return format_html('<span style="color:#64748b">sin correo · {}</span>', str(obj.pk)[:8])

    @admin.display(description="Membresías")
    def membership_count(self, obj: User) -> int:
        return getattr(obj, "_membership_count", obj.memberships.count())

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(_membership_count=Count("memberships", distinct=True))
        )


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = (
        "username",
        "organization",
        "user_label",
        "role",
        "status",
        "default_location",
        "last_used_at",
    )
    list_filter = ("role", "status", "organization")
    search_fields = (
        "username",
        "user__email",
        "user__first_name",
        "user__last_name",
        "organization__name",
        "organization__slug",
    )
    readonly_fields = ("created_at", "updated_at", "last_used_at")
    autocomplete_fields = ("user", "organization", "default_location")

    @admin.display(description="Usuario", ordering="user__email")
    def user_label(self, obj: Membership) -> str:
        user = obj.user
        if user.email:
            return user.email
        name = user.full_name
        return name or f"user:{str(user.pk)[:8]}"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "user", "organization", "default_location"
        )


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "organization", "role", "status", "expires_at", "accepted_at")
    list_filter = ("status", "role", "organization")
    search_fields = ("email", "organization__name")
    # El token está hasheado y no se puede leer: mostrarlo solo confundiría.
    exclude = ("token",)
    readonly_fields = ("created_at", "updated_at", "accepted_at", "membership")
    autocomplete_fields = ("organization", "invited_by", "membership")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("organization", "invited_by", "membership")
