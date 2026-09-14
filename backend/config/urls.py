from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.views.static import serve
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.core.docs import ApiReferenceView
from apps.core.health import HealthView

api_v1 = [
    path("health/", HealthView.as_view(), name="health"),
    path("auth/", include("apps.accounts.urls")),
    path("platform/", include("apps.platform.urls")),
    path("", include("apps.accounts.employee_urls")),
    path("", include("apps.organizations.urls")),
    path("", include("apps.subscriptions.urls")),
    path("", include("apps.catalog.urls")),
    path("", include("apps.inventory.urls")),
    path("", include("apps.customers.urls")),
    path("", include("apps.purchasing.urls")),
    path("", include("apps.cash.urls")),
    path("", include("apps.expenses.urls")),
    path("", include("apps.sales.urls")),
    path("", include("apps.synchronization.urls")),
    path("", include("apps.reporting.urls")),
    path("", include("apps.notifications.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include((api_v1, "v1"), namespace="v1")),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/v1/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/v1/reference/", ApiReferenceView.as_view(), name="api-reference"),
    # Uploaded files (product photos) live on local disk for now - there is no
    # CDN or object storage in front of this yet, so Django serves them
    # itself, unlike static/ which whitenoise already handles. Move this to
    # S3/Cloudinary + django-storages before scaling past one node.
    path(
        f"{settings.MEDIA_URL.lstrip('/')}<path:path>",
        serve,
        {"document_root": settings.MEDIA_ROOT},
    ),
]
