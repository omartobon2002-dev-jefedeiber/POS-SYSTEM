from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [  # noqa: F405
    "rest_framework.renderers.JSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",
]

# Any private-network origin may call the API in development: the frontend dev
# server usually runs on another machine (or another IP of this one) and on a
# port that changes, so an explicit allowlist means editing settings every time
# someone plugs in a laptop.
#
# Development only, on purpose. `production.py` keeps the explicit
# CORS_ALLOWED_ORIGINS list from the environment: these ranges are only
# "trusted" because a dev LAN is, and that assumption does not survive
# deployment. Credentials stay off (CORS_ALLOW_CREDENTIALS = False in base):
# auth travels in the Authorization header, not cookies, so a permissive
# origin cannot be used to ride an existing session.
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https?://localhost:\d+$",
    r"^https?://127\.0\.0\.1:\d+$",
    r"^https?://10\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?$",
    r"^https?://192\.168\.\d{1,3}\.\d{1,3}(:\d+)?$",
    r"^https?://172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(:\d+)?$",
    # Túneles HTTPS para probar PWA/push en el móvil
    r"^https://.*\.ngrok(-free)?\.(app|dev|io)$",
    r"^https://.*\.trycloudflare\.com$",
]
