import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .environment import fqdn_configuration, parse_bool


BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = parse_bool(os.environ.get("DJANGO_DEBUG"), name="DJANGO_DEBUG", default=True)
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is required when DJANGO_DEBUG is false.")
    SECRET_KEY = "django-insecure-local-development-only"

host_configuration = fqdn_configuration(
    os.environ.get("APP_FQDN"),
    os.environ.get(
        "DJANGO_ALLOWED_HOSTS",
        "localhost,127.0.0.1,testserver" if DEBUG else "",
    ),
    os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", ""),
)
APP_FQDN = host_configuration["fqdn"]
ALLOWED_HOSTS = host_configuration["allowed_hosts"]
CSRF_TRUSTED_ORIGINS = host_configuration["trusted_origins"]
if not DEBUG and not APP_FQDN:
    raise ImproperlyConfigured("APP_FQDN is required when DJANGO_DEBUG is false.")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "checklists",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *([] if DEBUG else ["whitenoise.middleware.WhiteNoiseMiddleware"]),
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "chore.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "checklists.context_processors.reporting_access",
            ],
        },
    },
]

WSGI_APPLICATION = "chore.wsgi.application"
ASGI_APPLICATION = "chore.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("CHORE_DATABASE_PATH", BASE_DIR / "db.sqlite3"),
        "OPTIONS": {
            "timeout": 20,
            # Acquire SQLite's write reservation when an atomic block begins. This
            # avoids deferred-transaction lock upgrades during shared checklist
            # creation and task state changes.
            "transaction_mode": "IMMEDIATE",
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "checklists.password_validation.OperationalAccountSimilarityValidator"},
    {"NAME": "checklists.password_validation.OperationalAccountMinimumLengthValidator"},
    {"NAME": "checklists.password_validation.OperationalAccountCommonPasswordValidator"},
    {"NAME": "checklists.password_validation.OperationalAccountNumericPasswordValidator"},
]

LANGUAGE_CODE = "en-ca"
TIME_ZONE = "America/Vancouver"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
SESSION_COOKIE_SECURE = parse_bool(
    os.environ.get("DJANGO_SESSION_COOKIE_SECURE"),
    name="DJANGO_SESSION_COOKIE_SECURE",
    default=not DEBUG,
)
CSRF_COOKIE_SECURE = parse_bool(
    os.environ.get("DJANGO_CSRF_COOKIE_SECURE"),
    name="DJANGO_CSRF_COOKIE_SECURE",
    default=not DEBUG,
)
SECURE_SSL_REDIRECT = parse_bool(
    os.environ.get("DJANGO_SECURE_SSL_REDIRECT"),
    name="DJANGO_SECURE_SSL_REDIRECT",
    default=not DEBUG,
)
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = parse_bool(
    os.environ.get("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS"),
    name="DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS",
    default=False,
)
SECURE_HSTS_PRELOAD = parse_bool(
    os.environ.get("DJANGO_SECURE_HSTS_PRELOAD"),
    name="DJANGO_SECURE_HSTS_PRELOAD",
    default=False,
)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

EMAIL_ENABLED = parse_bool(
    os.environ.get("EMAIL_ENABLED"), name="EMAIL_ENABLED", default=True
)
configured_email_backend = os.environ.get("EMAIL_BACKEND") or os.environ.get(
    "DJANGO_EMAIL_BACKEND"
)
if not EMAIL_ENABLED:
    EMAIL_BACKEND = "checklists.email_backends.DisabledEmailBackend"
elif configured_email_backend:
    EMAIL_BACKEND = configured_email_backend
elif DEBUG:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

EMAIL_HOST = os.environ.get("EMAIL_HOST") or os.environ.get(
    "DJANGO_EMAIL_HOST", "smtp.gmail.com"
)
EMAIL_PORT = int(
    os.environ.get("EMAIL_PORT") or os.environ.get("DJANGO_EMAIL_PORT", "587")
)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER") or os.environ.get(
    "DJANGO_EMAIL_HOST_USER", ""
)
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD") or os.environ.get(
    "DJANGO_EMAIL_HOST_PASSWORD", ""
)
EMAIL_USE_TLS = parse_bool(
    os.environ.get("EMAIL_USE_TLS") or os.environ.get("DJANGO_EMAIL_USE_TLS"),
    name="EMAIL_USE_TLS",
    default=True,
)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL") or os.environ.get(
    "DJANGO_DEFAULT_FROM_EMAIL", "chore@localhost"
)

CHORE_BACKUP_DIR = Path(
    os.environ.get("CHORE_BACKUP_DIR", BASE_DIR / "backups")
)
CHORE_BACKUP_RETENTION = int(os.environ.get("CHORE_BACKUP_RETENTION", "7"))
CHORE_BACKUPS_ENABLED = parse_bool(
    os.environ.get("CHORE_BACKUPS_ENABLED"),
    name="CHORE_BACKUPS_ENABLED",
    default=False,
)

LOG_LEVEL = os.environ.get("DJANGO_LOG_LEVEL", "INFO").upper()
LOGGING = {} if DEBUG else {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "{asctime} {levelname} {name}: {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        }
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.security.DisallowedHost": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        }
    },
}
