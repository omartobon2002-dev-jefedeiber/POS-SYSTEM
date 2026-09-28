"""Django admin for platform operators.

Tenant-scoped models must use ``all_objects`` here: the default ``objects``
manager requires an organization context and would raise
``TenantContextMissing`` when listing rows in the admin.
"""
from __future__ import annotations

from django.contrib import admin

from .models import AuditLog, DocumentSequence, IdempotencyKey


class UnscopedRelationsMixin:
    """Relation fields in admin forms read through ``all_objects``.

    A foreign key to a tenant-scoped model (e.g. a membership's
    ``default_location``) builds its choices from the related model's default
    manager, which refuses to run without an organization context - and the
    admin has none. Platform operators see every tenant anyway.
    """

    @staticmethod
    def _unscoped(db_field, kwargs):
        related = db_field.remote_field.model
        if "queryset" not in kwargs and hasattr(related, "all_objects"):
            kwargs["queryset"] = related.all_objects.all()
        return kwargs

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        return super().formfield_for_foreignkey(db_field, request, **self._unscoped(db_field, kwargs))

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        return super().formfield_for_manytomany(db_field, request, **self._unscoped(db_field, kwargs))


class UnscopedInlineMixin(UnscopedRelationsMixin):
    """Inline rows of a tenant-scoped model, read without an organization context."""

    def get_queryset(self, request):
        model = self.model
        queryset = model.all_objects.all() if hasattr(model, "all_objects") else model._default_manager.all()
        ordering = self.get_ordering(request)
        return queryset.order_by(*ordering) if ordering else queryset


class UnscopedTenantAdmin(UnscopedRelationsMixin, admin.ModelAdmin):
    """Base for models that inherit ``TenantScopedModel``.

    Reads and writes through ``all_objects`` so the platform admin can see
    every organization without wrapping each request in ``tenant_context``.
    """

    select_related_fields: tuple[str, ...] = ("organization",)

    def get_queryset(self, request):
        qs = self.model.all_objects.get_queryset()
        related = getattr(self, "select_related_fields", ())
        if related:
            qs = qs.select_related(*related)
        return qs


class ReadOnlyAdminMixin:
    """Block creates/deletes for ledger-like rows that must stay immutable."""

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdminMixin, UnscopedTenantAdmin):
    list_display = ("created_at", "organization", "action", "actor_label", "object_type", "object_id")
    list_filter = ("action", "organization")
    search_fields = ("actor_label", "object_id", "action")
    readonly_fields = [f.name for f in AuditLog._meta.fields]
    select_related_fields = ("organization", "actor")
    date_hierarchy = "created_at"

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(ReadOnlyAdminMixin, UnscopedTenantAdmin):
    list_display = ("key", "organization", "endpoint", "status", "response_status", "created_at", "completed_at")
    list_filter = ("status", "organization")
    search_fields = ("key", "endpoint")
    readonly_fields = [f.name for f in IdempotencyKey._meta.fields]
    select_related_fields = ("organization",)
    date_hierarchy = "created_at"

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(UnscopedTenantAdmin):
    list_display = ("organization", "location", "document_type", "prefix", "last_number")
    list_filter = ("document_type", "organization")
    search_fields = ("prefix", "location__name", "organization__name")
    select_related_fields = ("organization", "location")
    autocomplete_fields = ("organization", "location")
