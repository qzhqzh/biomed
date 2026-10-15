"""Django settings for biomed."""

from pathlib import Path
import os
import sys

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


IS_TESTING = "test" in sys.argv or "pytest" in Path(sys.argv[0]).name.lower()
DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if DEBUG or IS_TESTING:
        SECRET_KEY = "dev-only-online-biotools-secret-key"
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is required when DJANGO_DEBUG is disabled"
        )

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1"
    ).split(",")
    if host.strip()
]
if IS_TESTING and "testserver" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("testserver")

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

# TLS and reverse-proxy settings are explicit opt-ins. Enable them only when
# Django is behind a trusted proxy that overwrites the corresponding headers.
TRUST_X_FORWARDED_PROTO = env_bool("DJANGO_TRUST_X_FORWARDED_PROTO", False)
if TRUST_X_FORWARDED_PROTO:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", SECURE_SSL_REDIRECT)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", SECURE_SSL_REDIRECT)
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool(
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", False
)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)
SECURE_CONTENT_TYPE_NOSNIFF = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apps.portal",
    "apps.evidence",
    "apps.curation",
    "apps.annotations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "apps.annotations.middleware.RequestContextMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

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

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "OPTIONS": {"timeout": 30, "transaction_mode": "IMMEDIATE"},
        "NAME": os.environ.get(
            "DATABASE_PATH",
            os.environ.get("DJANGO_DB_PATH", str(BASE_DIR / "var/django/db.sqlite3")),
        ),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# This project is currently served over plain HTTP on a trusted LAN. Browsers
# ignore COOP on non-trustworthy origins, so do not emit a misleading header.
SECURE_CROSS_ORIGIN_OPENER_POLICY = None
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_THROTTLE_RATES": {
        "annotation": os.environ.get("BIOTOOLS_ANNOTATION_RATE", "30/min"),
    },
}

# Request limits
DATA_UPLOAD_MAX_MEMORY_SIZE = int(
    os.environ.get("DATA_UPLOAD_MAX_MEMORY_SIZE", str(2 * 1024 * 1024))
)
DATA_UPLOAD_MAX_NUMBER_FIELDS = int(os.environ.get("DATA_UPLOAD_MAX_NUMBER_FIELDS", "1000"))

# Auth / ops
# Comma-separated keys. When non-empty (or BIOTOOLS_REQUIRE_API_KEY=1), POST annotate requires X-API-Key.
BIOTOOLS_API_KEYS = os.environ.get("BIOTOOLS_API_KEYS", "")
BIOTOOLS_REQUIRE_API_KEY = env_bool("BIOTOOLS_REQUIRE_API_KEY", False)
# Trust X-Forwarded-For only behind a proxy that strips the client-supplied header.
BIOTOOLS_TRUST_X_FORWARDED_FOR = env_bool(
    "BIOTOOLS_TRUST_X_FORWARDED_FOR", False
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
        },
    },
    "loggers": {
        "biotools.access": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "biotools.jobs": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

# --- Local knowledge base (NCBI gene_info + MANE) ---
KNOWLEDGE_DIR = os.environ.get(
    "KNOWLEDGE_DIR",
    str(BASE_DIR / "data" / "knowledge"),
)
KNOWLEDGE_UPDATES_DIR = os.environ.get(
    "KNOWLEDGE_UPDATES_DIR", str(BASE_DIR / "data" / "knowledge-updates")
)

# --- Annotation engines ---
VEP_BIN = os.environ.get("VEP_BIN", "/opt/vep/src/ensembl-vep/vep")
VEP_CACHE_DIR = os.environ.get("VEP_CACHE_DIR", str(BASE_DIR / "data" / "vep"))
VEP_ASSEMBLY_DEFAULT = os.environ.get("VEP_ASSEMBLY", "GRCh37")
VEP_TIMEOUT_SECONDS = int(os.environ.get("VEP_TIMEOUT_SECONDS", "300"))
VEP_MAX_CONCURRENCY = int(os.environ.get("VEP_MAX_CONCURRENCY", "2"))
# auto: use local VEP_BIN when present, otherwise Docker
VEP_MODE = os.environ.get("VEP_MODE", "auto")  # auto|local|docker
VEP_DOCKER_IMAGE = os.environ.get(
    "VEP_DOCKER_IMAGE",
    "ensemblorg/ensembl-vep:release_116.0",
)
ANNOTATION_MAX_VARIANTS = int(os.environ.get("ANNOTATION_MAX_VARIANTS", "200"))
# Minimum seconds between annotation job submissions per client (IP / API key).
BIOTOOLS_JOB_COOLDOWN_SECONDS = int(os.environ.get("BIOTOOLS_JOB_COOLDOWN_SECONDS", "10"))
BIOTOOLS_JOB_STALE_AFTER_SECONDS = int(
    os.environ.get("BIOTOOLS_JOB_STALE_AFTER_SECONDS", "1800")
)
BIOTOOLS_WORKER_POLL_SECONDS = float(
    os.environ.get("BIOTOOLS_WORKER_POLL_SECONDS", "1")
)

# assembly -> relative cache subdirectory under VEP_CACHE_DIR (manifest-aligned)
VEP_ASSEMBLY_CACHE = {
    "GRCh37": os.environ.get("VEP_CACHE_GRCh37", "homo_sapiens_merged/116_GRCh37"),
    "GRCh38": os.environ.get("VEP_CACHE_GRCh38", "homo_sapiens_merged/116_GRCh38"),
}

# ANNOVAR (local perl or dockerized CLI)
ANNOVAR_MODE = os.environ.get("ANNOVAR_MODE", "auto")  # auto|local|docker
ANNOVAR_DB_DIR = os.environ.get(
    "ANNOVAR_DB_DIR",
    str(BASE_DIR / "data" / "annovar" / "humandb"),
)
ANNOVAR_TABLE_BIN = os.environ.get(
    "ANNOVAR_TABLE_BIN",
    "/home/TOOLS/tools/annovar/current/bin/table_annovar.pl",
)
ANNOVAR_DOCKER_IMAGE = os.environ.get(
    "ANNOVAR_DOCKER_IMAGE",
    "registry.cn-shanghai.aliyuncs.com/kszy-biosoft/annovar:v20180416_2",
)
ANNOVAR_TIMEOUT_SECONDS = int(os.environ.get("ANNOVAR_TIMEOUT_SECONDS", "300"))
ANNOVAR_MAX_CONCURRENCY = int(os.environ.get("ANNOVAR_MAX_CONCURRENCY", "1"))
ANNOVAR_VERSION_LABEL = os.environ.get("ANNOVAR_VERSION_LABEL", "2018-04-16")
# Gate public/online ANNOVAR usage until license is confirmed by the operator.
ANNOVAR_PUBLIC_ENABLED = env_bool("ANNOVAR_PUBLIC_ENABLED", False)

# Internal evidence service; the browser only uses Django on the public port.
EVIDENCE_API_URL = os.environ.get("EVIDENCE_API_URL", "http://127.0.0.1:18765")

# Versioned document storage and the optional, explicitly configured model adapter.
CURATION_STORAGE_DIR = Path(os.environ.get("CURATION_STORAGE_DIR", BASE_DIR / "var" / "library"))
CURATION_MODEL_BACKEND = os.environ.get("CURATION_MODEL_BACKEND", "")
CURATION_POLICY_VERSION = "ai-publish-human-audit-v1"
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
FILE_UPLOAD_HANDLERS = [
    "apps.curation.uploads.BoundedUploadHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]
