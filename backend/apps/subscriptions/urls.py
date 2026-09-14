from rest_framework.routers import DefaultRouter

from .views import CurrentSubscriptionViewSet

router = DefaultRouter()
router.register("subscription", CurrentSubscriptionViewSet, basename="subscription")

urlpatterns = router.urls
