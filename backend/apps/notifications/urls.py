from django.urls import path

from .views import VapidPublicKeyView, WebPushSubscriptionView

urlpatterns = [
    path(
        "notifications/vapid-public-key/",
        VapidPublicKeyView.as_view(),
        name="vapid-public-key",
    ),
    path(
        "notifications/subscriptions/",
        WebPushSubscriptionView.as_view(),
        name="web-push-subscriptions",
    ),
]
