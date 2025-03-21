"""
Django settings for updater_api project.

Generated by 'django-admin startproject' using Django 3.1.1.

For more information on this file, see
https://docs.djangoproject.com/en/3.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/3.1/ref/settings/
"""

import datetime
import json
import os
import tomllib
import warnings
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve(strict=True).parent.parent.parent
ENV_FILE = os.path.join(BASE_DIR, ".env")
LOG_DIR = os.path.join(BASE_DIR, "logs")
BUILD_FILE = Path(f"{BASE_DIR}/BUILD.txt")

# .env
if os.path.exists(ENV_FILE):
    load_dotenv(dotenv_path=ENV_FILE, verbose=True)

INSTANCE_NAME = os.getenv("INSTANCE_NAME", "evilflowers.local")

if BUILD_FILE.exists():
    with open(BUILD_FILE) as f:
        BUILD = f.readline().replace("\n", "")
else:
    BUILD = datetime.datetime.now().isoformat()

with open(BASE_DIR / "pyproject.toml", "rb") as f:
    pyproject = tomllib.load(f)
    VERSION = pyproject["tool"]["poetry"]["version"]


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/3.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv("SECRET_KEY")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "").split(";") + ["localhost"]


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.postgres",
    "corsheaders",
    "django_api_forms",
    "apps.core",
    "apps.api",
    "apps.files",
    "apps.opds",
    "apps.opds2",
    "apps.openapi",
    "apps.tasks",
    "apps.readium",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "apps.api.middleware.exceptions.ExceptionMiddleware",
]

ROOT_URLCONF = "evil_flowers_catalog.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "evil_flowers_catalog.wsgi.application"


# Database
# https://docs.djangoproject.com/en/3.1/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.getenv("PGHOST"),
        "PORT": os.getenv("PGPORT", 5432),
        "NAME": os.getenv("PGDATABASE"),
        "USER": os.getenv("PGUSER"),
        "PASSWORD": os.getenv("PGPASSWORD", None),
        "OPTIONS": {"pool": True},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

# Password validation
# https://docs.djangoproject.com/en/3.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]

AUTH_USER_MODEL = "core.User"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
]

OBJECT_CHECKERS_MODULE = "apps.core.checkers"

SECURED_VIEW_AUTHENTICATION_SCHEMAS = {
    "Basic": "apps.core.auth.BasicBackend",
    "Bearer": "apps.core.auth.BearerBackend",
}

SECURED_VIEW_JWT_ALGORITHM = "RS256"
SECURED_VIEW_JWK = json.loads(os.getenv("SECURED_VIEW_JWK")) if "SECURED_VIEW_JWK" in os.environ else None
SECURED_VIEW_JWT_ACCESS_TOKEN_EXPIRATION = timedelta(
    minutes=int(os.getenv("SECURED_VIEW_JWT_ACCESS_TOKEN_EXPIRATION", 5))
)
SECURED_VIEW_JWT_REFRESH_TOKEN_EXPIRATION = timedelta(
    minutes=int(os.getenv("SECURED_VIEW_JWT_REFRESH_TOKEN_EXPIRATION", 60 * 24))
)

# Internationalization
# https://docs.djangoproject.com/en/3.1/topics/i18n/

LANGUAGE_CODE = "en"

TIME_ZONE = "UTC"

DATETIME_INPUT_FORMATS = ("%Y-%m-%dT%H:%M:%S%z",)

USE_I18N = True

USE_L10N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.1/howto/static-files/

STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "static")

MEDIA_URL = "/media/"
MEDIA_ROOT = os.getenv("MEDIA_ROOT", os.path.join(BASE_DIR, "media"))

DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024 * 100  # 100MB

# Sentry
if os.getenv("SENTRY_DSN", False):

    def before_send(event, hint):
        if "exc_info" in hint:
            exc_type, exc_value, tb = hint["exc_info"]
            if exc_type.__name__ in ["ValidationException", "DisallowedHost"]:
                return None
        if "extra" in event and not event["extra"].get("to_sentry", True):
            return None

        return event

    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration

        sentry_sdk.init(
            integrations=[DjangoIntegration()],
            attach_stacktrace=True,
            send_default_pii=True,
            before_send=before_send,
            release=VERSION,
        )
    except ImportError:
        warnings.warn("sentry_sdk module is not installed")

# Redis
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DATABASE = int(os.getenv("REDIS_DATABASE", "0"))

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DATABASE}",
        "KEY_PREFIX": "evilflowers",
    }
}

# Pagination
EVILFLOWERS_PAGINATION_DEFAULT_LIMIT = int(os.getenv("EVILFLOWERS_PAGINATION_DEFAULT_LIMIT", 10))

# Images & thumbnails
EVILFLOWERS_IMAGE_UPLOAD_MAX_SIZE = int(os.getenv("EVILFLOWERS_IMAGE_UPLOAD_MAX_SIZE", 5)) * 1024 * 1024  # MB
EVILFLOWERS_IMAGE_MIME = (
    "image/gif",
    "image/jpeg",
    "image/png",
)
EVILFLOWERS_IMAGE_THUMBNAIL = (768, 480)

EVILFLOWERS_FEEDS_NEW_LIMIT = os.getenv("EVILFLOWERS_FEEDS_NEW_LIMIT", 20)

EVILFLOWERS_IDENTIFIERS = ["isbn", "google", "doi"]

EVILFLOWERS_ENFORCE_USER_ACQUISITIONS = bool(int(os.getenv("EVILFLOWERS_ENFORCE_USER_ACQUISITIONS", "0")))

EVILFLOWERS_USER_ACQUISITION_MODE = os.getenv("EVILFLOWERS_USER_ACQUISITION_MODE", "single")

# Storage
EVILFLOWERS_STORAGE_DRIVER = os.getenv("EVILFLOWERS_STORAGE_DRIVER", "apps.files.storage.filesystem.FileSystemStorage")
EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR = os.getenv(
    "EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR", BASE_DIR / "data/evilflowers/storage"
)
EVILFLOWERS_STORAGE_S3_HOST = os.getenv("EVILFLOWERS_STORAGE_S3_HOST")
EVILFLOWERS_STORAGE_S3_ACCESS_KEY = os.getenv("EVILFLOWERS_STORAGE_S3_ACCESS_KEY")
EVILFLOWERS_STORAGE_S3_SECRET_KEY = os.getenv("EVILFLOWERS_STORAGE_S3_SECRET_KEY")
EVILFLOWERS_STORAGE_S3_SECURE = bool(int(os.getenv("EVILFLOWERS_STORAGE_S3_SECURE", 0)))
EVILFLOWERS_STORAGE_S3_BUCKET = os.getenv("EVILFLOWERS_STORAGE_S3_BUCKET")

# Readium
EVILFLOWERS_READIUM_DATADIR = str(os.getenv("EVILFLOWERS_READIUM_DATADIR", BASE_DIR / "data/evilflowers/readium"))
EVILFLOWERS_READIUM_LCPSV_URL = os.getenv("EVILFLOWERS_READIUM_LCPSV_URL", "http://127.0.0.1:8989")
EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL = os.getenv(
    "EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL", "http://127.0.0.1:8989"
)
EVILFLOWERS_READIUM_BASE_URL = os.getenv(
    "EVILFLOWERS_READIUM_BASE_URL",
    f"http://{os.getenv('DJANGO_RUNSERVER_IP', '127.0.0.1')}:{os.getenv('DJANGO_RUNSERVER_PORT', '8000')}",
)

# Cache
EVILFLOWERS_CACHE_SERVER_HASHES = timedelta(minutes=int(os.getenv("EVILFLOWERS_CACHE_HASHES", 7 * 24 * 60)))
EVILFLOWERS_CACHE_SERVER_API_KEYS = timedelta(minutes=int(os.getenv("EVILFLOWERS_CACHE_API_KEYS", 0)))
EVILFLOWERS_CACHE_CLIENT_IMAGES = timedelta(minutes=int(os.getenv("EVILFLOWERS_CACHE_CLIENT_IMAGES", 24 * 60)))

# Modifiers
EVILFLOWERS_MODIFIERS = {"application/pdf": "apps.core.modifiers.pdf.PDFModifier"}

# Admin
EVILFLOWERS_CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "root@localhost")

# OpenAPI
EVILFLOWERS_OPENAPI_APPS = ["api", "files", "readium"]

# Backups
EVILFLOWERS_BACKUP_PGDUMP_BIN = os.getenv("EVILFLOWERS_BACKUP_PGDUMP_BIN", "pg_dump")
EVILFLOWERS_BACKUP_SCHEDULE = os.getenv("EVILFLOWERS_BACKUP_SCHEDULE")
EVILFLOWERS_BACKUP_DESTINATION = os.getenv("EVILFLOWERS_BACKUP_DESTINATION")
EVILFLOWERS_BACKUP_S3_HOST = os.environ.get("EVILFLOWERS_BACKUP_S3_HOST")
EVILFLOWERS_BACKUP_S3_ACCESS_KEY = os.environ.get("EVILFLOWERS_BACKUP_S3_ACCESS_KEY")
EVILFLOWERS_BACKUP_S3_SECRET_KEY = os.environ.get("EVILFLOWERS_BACKUP_S3_SECRET_KEY")
EVILFLOWERS_BACKUP_S3_SECURE = os.environ.get("EVILFLOWERS_BACKUP_S3_SECURE", "1").lower() == "1"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
        "syslog": {"class": "logging.handlers.SysLogHandler"},
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}

CORS_ALLOW_ALL_ORIGINS = True
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "0") == "1"

if os.getenv("ELASTIC_APM_SERVICE_NAME"):
    INSTALLED_APPS.append("elasticapm.contrib.django")
    LOGGING["handlers"]["elasticapm"] = {
        "level": "INFO",
        "class": "elasticapm.contrib.django.handlers.LoggingHandler",
    }
    LOGGING["root"]["handlers"].append("elasticapm")
    MIDDLEWARE.append("elasticapm.contrib.django.middleware.TracingMiddleware")
    ELASTIC_APM = {
        "DEBUG": True,
    }


CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DATABASE}")
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TIMEZONE = os.getenv("CELERY_TIMEZONE", TIME_ZONE)
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", 30 * 60))  # Default: 30min
CELERY_TASK_ROUTES = {
    "evilflowers_ocr_worker.*": {"queue": "evilflowers_ocr_worker"},
    "evilflowers_lcpencrypt_worker.*": {"queue": "evilflowers_lcpencrypt_worker"},
}
