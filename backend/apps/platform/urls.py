from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import PlatformLoginView, PlatformOrganizationViewSet

router = DefaultRouter()
router.register("organizations", PlatformOrganizationViewSet, basename="platform-organization")

urlpatterns = [
    path("auth/login/", PlatformLoginView.as_view(), name="platform-login"),
] + router.urls
