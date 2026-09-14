from django.contrib import admin

from .models import Location, Organization


class LocationInline(admin.TabularInline):
    model = Location
    extra = 0
    fields = ("name", "code", "is_default", "is_active", "address", "city")
    show_change_link = True


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "tax_id", "currency", "is_active", "created_at")
    search_fields = ("name", "slug", "tax_id")
    list_filter = ("is_active", "country", "currency")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [LocationInline]


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "organization", "is_default", "is_active")
    list_filter = ("is_active", "is_default", "organization")
    search_fields = ("name", "code", "organization__name", "organization__slug")
    autocomplete_fields = ("organization",)

    def get_queryset(self, request):
        return Location.all_objects.select_related("organization")
