"""
Django settings for the TaskPilot API.

Everything environment-specific comes from environment variables. In
development they can live in backend/.env (gitignored; see .env.example and
scripts/setup-local-db.ps1). Real environment variables always win over .env.

DJANGO_ENV=development | production selects safe defaults; production refuses
to start with insecure or missing settings.
"""

import os
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Minimal .env reader: KEY=value lines; never overrides real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def env(name, default=None, *, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(f"Environment variable {name} is required.")
    return value


def env_bool(name, default=False):
    return str(env(name, str(default))).lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


DJANGO_ENV = env("DJANGO_ENV", "development")
if DJANGO_ENV not in {"development", "production"}:
    raise ImproperlyConfigured("DJANGO_ENV must be 'development' or 'production'.")
IS_PRODUCTION = DJANGO_ENV == "production"
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"
COLLECTSTATIC = any("collectstatic" in arg for arg in sys.argv)

DEBUG = env_bool("DJANGO_DEBUG", not IS_PRODUCTION)
if IS_PRODUCTION and DEBUG and not COLLECTSTATIC:
    raise ImproperlyConfigured("DJANGO_DEBUG must be off in production.")

SECRET_KEY = env("DJANGO_SECRET_KEY", required=IS_PRODUCTION and not COLLECTSTATIC) or "dev-only-insecure-key-do-not-use-in-production-fallback-for-build-and-testing-12345"
if IS_PRODUCTION and not COLLECTSTATIC and (len(SECRET_KEY) < 50 or SECRET_KEY.startswith("dev-only")):
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be a long random value in production.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "" if (IS_PRODUCTION and not COLLECTSTATIC) else "localhost,127.0.0.1,*")
if IS_PRODUCTION and not COLLECTSTATIC and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS is required in production.")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "drf_spectacular",
    "accounts",
    "workspaces",
    "projects",
    "sprints",
    "issues",
    "activity",
    "notifications",
    "reports",
]

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
]

ROOT_URLCONF = "taskpilot.urls"

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

WSGI_APPLICATION = "taskpilot.wsgi.application"

# PostgreSQL only: the app relies on its constraints, JSON fields and locking.
_db_url = env("DATABASE_URL") or env("POSTGRES_URL")
if _db_url:
    _parsed = urlsplit(_db_url)
    _db_name = _parsed.path.lstrip("/") or "postgres"
    _db_user = unquote(_parsed.username or "")
    _db_password = unquote(_parsed.password or "")
    _db_host = _parsed.hostname or "localhost"
    _db_port = str(_parsed.port or 5432)
    _query_params = parse_qs(_parsed.query)
    _db_sslmode = _query_params.get("sslmode", [env("POSTGRES_SSLMODE", "require" if IS_PRODUCTION else "prefer")])[0]
else:
    _db_name = env("POSTGRES_DB", "taskpilot")
    _db_user = env("POSTGRES_USER", "taskpilot")
    _db_password = env("POSTGRES_PASSWORD", required=not COLLECTSTATIC) or "build-only-db-pass"
    _db_host = env("POSTGRES_HOST", "localhost")
    _db_port = env("POSTGRES_PORT", "5432")
    _db_sslmode = env("POSTGRES_SSLMODE", "require" if IS_PRODUCTION else "prefer")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _db_name,
        "USER": _db_user,
        "PASSWORD": _db_password,
        "HOST": _db_host,
        "PORT": _db_port,
        "CONN_MAX_AGE": int(env("POSTGRES_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"sslmode": _db_sslmode},
    }
}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = Path(env("DJANGO_STATIC_ROOT", str(BASE_DIR / "staticfiles")))
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# Uploaded attachments. Never served directly: downloads go through a signed,
# short-lived API URL (issues/views.py), so MEDIA_URL isn't routed.
MEDIA_ROOT = Path(env("DJANGO_MEDIA_ROOT", str(BASE_DIR / "media")))
ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024
ATTACHMENT_URL_MAX_AGE = 900  # seconds a signed download link stays valid (the UI refetches every 10 min)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------- Frontend, CORS, CSRF ----------

FRONTEND_URL = env("FRONTEND_URL", "" if (IS_PRODUCTION and not COLLECTSTATIC) else "http://localhost:5173", required=IS_PRODUCTION and not COLLECTSTATIC).rstrip("/")
# Only these origins may call the API from a browser (credentials allowed for the refresh cookie).
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "" if (IS_PRODUCTION and not COLLECTSTATIC) else "http://localhost:5173")
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", ",".join(CORS_ALLOWED_ORIGINS))

INVITATION_TTL = timedelta(days=int(env("INVITATION_TTL_DAYS", "7")))
PASSWORD_RESET_TIMEOUT = int(env("PASSWORD_RESET_TIMEOUT", str(60 * 60 * 3)))

# ---------- Email ----------

EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend" if IS_PRODUCTION else "django.core.mail.backends.console.EmailBackend",
)
EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DJANGO_DEFAULT_FROM_EMAIL", "TaskPilot <no-reply@taskpilot.local>")

# ---------- Cache ----------
# Throttle counters and the refresh-rotation grace window live here. They must
# be shared by every worker process: production defaults to a PostgreSQL table
# (run `manage.py createcachetable` on deploy); development uses process memory.
CACHE_BACKEND = env("DJANGO_CACHE", "database" if IS_PRODUCTION else "locmem")
CACHES = {
    "default": (
        {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "taskpilot_cache"}
        if CACHE_BACKEND == "database"
        else {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    )
}

# ---------- REST framework ----------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework_simplejwt.authentication.JWTAuthentication"],
    # Deny by default: every view must opt in to anything more permissive
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PAGINATION_CLASS": "taskpilot.pagination.StandardPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_RATES": {
        "auth": env("THROTTLE_AUTH", "20/min"),
        # Every page load refreshes once; a valid httpOnly cookie is required anyway
        "refresh": env("THROTTLE_REFRESH", "120/min"),
        "invitation": env("THROTTLE_INVITATION", "30/min"),
        "password_reset": env("THROTTLE_PASSWORD_RESET", "5/hour"),
    },
    "EXCEPTION_HANDLER": "rest_framework.views.exception_handler",
    # Behind N reverse proxies, throttles must read the client IP from X-Forwarded-For
    "NUM_PROXIES": int(env("DRF_NUM_PROXIES", "0")) or None,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(env("JWT_ACCESS_MINUTES", "10"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(env("JWT_REFRESH_DAYS", "14"))),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "SIGNING_KEY": env("JWT_SIGNING_KEY", SECRET_KEY),
}

# The refresh token lives only in this httpOnly cookie (never in JS or the body);
# the short-lived access token is returned in JSON and kept in memory.
REFRESH_COOKIE = {
    "name": env("REFRESH_COOKIE_NAME", "tp_refresh"),
    "path": "/api/auth/",
    "secure": env_bool("REFRESH_COOKIE_SECURE", IS_PRODUCTION),
    "samesite": env("REFRESH_COOKIE_SAMESITE", "Lax"),
    "domain": env("REFRESH_COOKIE_DOMAIN", "") or None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "TaskPilot API",
    "DESCRIPTION": "Workspaces, projects, issues, sprints, activity, notifications and reports. "
    "Every endpoint under /workspaces/{id}/ is authorized against the caller's membership.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": r"/api/",
    "ENUM_NAME_OVERRIDES": {
        "WorkspaceRoleEnum": "workspaces.roles.WorkspaceRole",
        "ProjectRoleEnum": "workspaces.roles.ProjectRole",
        "IssueStatusEnum": "issues.models.Issue.Status",
        "SprintStatusEnum": "sprints.models.Sprint.Status",
        "ProjectStatusEnum": "projects.models.Project.Status",
    },
}
# Interactive docs are on in development; opt in explicitly in production.
API_DOCS_ENABLED = env_bool("API_DOCS_ENABLED", not IS_PRODUCTION)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
}

# ---------- Production hardening ----------

if IS_PRODUCTION and not COLLECTSTATIC:
    if not CORS_ALLOWED_ORIGINS:
        raise ImproperlyConfigured("CORS_ALLOWED_ORIGINS must list the frontend origin(s) in production.")
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    # Preloading is a long-term commitment for the whole domain: opt in explicitly
    SECURE_HSTS_PRELOAD = env_bool("DJANGO_HSTS_PRELOAD", False)
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    if not REFRESH_COOKIE["secure"]:
        raise ImproperlyConfigured("The refresh cookie must be Secure in production.")

if TESTING:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    MEDIA_ROOT = BASE_DIR / ".test-media"
    # Tests make many logins from one "client"; throttling has its own test
    REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {scope: "10000/min" for scope in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]}
    EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
