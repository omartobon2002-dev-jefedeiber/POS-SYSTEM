"""Base settings shared by every environment."""
from datetime import timedelta
from pathlib import Path

import environ
from corsheaders.defaults import default_headers

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-key-do-not-use-in-production")
DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "django_filters",
    "corsheaders",
    "drf_spectacular",
]

LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.organizations",
    "apps.subscriptions",
    "apps.catalog",
    "apps.inventory",
    "apps.customers",
    "apps.purchasing",
    "apps.cash",
    "apps.expenses",
    "apps.sales",
    "apps.synchronization",
    "apps.reporting",
    "apps.platform",
    "apps.notifications",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Guarantees the tenant contextvar never leaks between requests reusing a worker.
    "apps.core.middleware.TenantContextMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", default="pos"),
        "USER": env("DB_USER", default="pos"),
        "PASSWORD": env("DB_PASSWORD", default="pos"),
        "HOST": env("DB_HOST", default="localhost"),
        "PORT": env("DB_PORT", default="5432"),
        "ATOMIC_REQUESTS": False,
        "CONN_MAX_AGE": env.int("DB_CONN_MAX_AGE", default=60),
    }
}

AUTH_USER_MODEL = "accounts.User"

# El correo es la identidad global, así que `ModelBackend` podría resolverlo,
# pero solo debe hacerlo para el admin: las credenciales de tienda (usuario +
# contraseña dentro de un negocio) se verifican en apps.accounts.services, que
# siempre parte de una organización. Este backend sirve únicamente el login
# del admin.
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.PlatformStaffBackend"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "es-co"
TIME_ZONE = "America/Bogota"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
# Uploads are bounded so a tenant cannot exhaust worker memory with a single request.
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.core.authentication.OrganizationJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.DefaultPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "auth": "10/min",
        "register": "5/hour",
        "write": "120/min",
        # A till catching up after hours offline sends batches, not thousands
        # of single requests, so this is generous enough.
        "sync": "60/min",
        # La pantalla de bloqueo de una caja consulta su personal al arrancar y
        # tras cada cierre de turno, no en bucle.
        "device": "30/min",
    },
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_TOKEN_LIFETIME_MINUTES", default=60)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_TOKEN_LIFETIME_DAYS", default=30)),
    "ROTATE_REFRESH_TOKENS": False,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Retail POS SaaS API",
    "DESCRIPTION": "Multi-tenant backend for fashion retail point of sale.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    "SERVERS": [
        {"url": "http://localhost:8000", "description": "Local development"},
    ],
    # Several domains have their own "status"; name each enum after its owner
    # instead of letting the generator invent StatusD73Enum.
    "ENUM_NAME_OVERRIDES": {
        "SaleStatusEnum": "apps.sales.models.SALE_STATUS_CHOICES",
        "PurchaseStatusEnum": "apps.purchasing.models.PURCHASE_STATUS_CHOICES",
        "CashSessionStatusEnum": "apps.cash.models.CASH_SESSION_STATUS_CHOICES",
        "SubscriptionStatusEnum": "apps.subscriptions.models.SUBSCRIPTION_STATUS_CHOICES",
        "UserStatusEnum": "apps.accounts.models.USER_STATUS_CHOICES",
        "MembershipRoleEnum": "apps.accounts.models.MEMBERSHIP_ROLE_CHOICES",
        "MembershipStatusEnum": "apps.accounts.models.MEMBERSHIP_STATUS_CHOICES",
        "InvitationStatusEnum": "apps.accounts.models.INVITATION_STATUS_CHOICES",
        "PaymentMethodEnum": "apps.core.enums.PAYMENT_METHOD_CHOICES",
        "SyncOperationStatusEnum": "apps.synchronization.models.SYNC_OPERATION_STATUS_CHOICES",
    },
}

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=["http://localhost:5173"])
CORS_ALLOW_CREDENTIALS = False

# Idempotency-Key is mandatory on POST /sales/ and /refunds/, so a browser
# client cannot work at all unless the preflight allows it: it is not in
# django-cors-headers' default header list.
CORS_ALLOW_HEADERS = (*default_headers, "idempotency-key", "x-device-token")

# Lets the client tell a replayed response from a freshly executed one - the
# difference matters when deciding whether to print a receipt again.
CORS_EXPOSE_HEADERS = ["Idempotent-Replay"]

# Invitaciones. `FRONTEND_URL` es la base del enlace que se envía por correo;
# el backend nunca sirve esa pantalla, solo valida el token que vuelve.
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@pos.local")
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:5173").rstrip("/")
INVITATION_TTL_DAYS = env.int("INVITATION_TTL_DAYS", default=7)

# Web Push (VAPID). Leave empty to disable sale notifications until configured.
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", default="")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", default="")
VAPID_SUBJECT = env("VAPID_SUBJECT", default="mailto:admin@example.com")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "handlers": ["console"], "propagate": False},
    },
}
