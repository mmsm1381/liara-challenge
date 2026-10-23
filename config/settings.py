"""Settings for the Liara Assistant. Everything that differs per deploy comes from env vars."""

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_rq",
    "assistant",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
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
                "assistant.context_processors.assets",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=60
    )
}

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
RQ_QUEUES = {"default": {"URL": REDIS_URL, "DEFAULT_TIMEOUT": 1800}}
# Tests and single-process dev can run jobs inline.
RQ_SYNC = env_bool("RQ_SYNC", False)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

LANGUAGE_CODE = "fa"
TIME_ZONE = "Asia/Tehran"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Uploaded ZIPs live here only until the deploy job has packed them.
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", BASE_DIR / "var" / "uploads"))
MAX_ZIP_BYTES = int(os.environ.get("MAX_ZIP_BYTES", 50 * 1024 * 1024))
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# Fernet key for Liara tokens. Required outside DEBUG.
TOKEN_ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY", "")

LIARA_API_BASE = os.environ.get("LIARA_API_BASE", "https://api.iran.liara.ir")
LIARA_STORAGE_API_BASE = os.environ.get("LIARA_STORAGE_API_BASE", "https://storage-service.liara.ir")
LIARA_TIMEOUT = float(os.environ.get("LIARA_TIMEOUT", 20))
# Used to tell the fake client apart in tests and demos without an account.
LIARA_FAKE = env_bool("LIARA_FAKE", False)

# Optional read-only GitHub token for downloading public repos (anonymous limit: 60/hour/IP).
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Liara AI (OpenAI-compatible). Swappable through these three vars.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-4o-mini")
LLM_TOOL_MODE = os.environ.get("LLM_TOOL_MODE", "native")  # native | prompt_json
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", 30))
LLM_MAX_TOOL_CALLS = 8
LLM_MAX_SECONDS = 60
LLM_DAILY_CAP = int(os.environ.get("LLM_DAILY_CAP", 200))

APPROVAL_TTL_SECONDS = 15 * 60

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    # Never log request bodies: the key POST must not reach the logs.
    "loggers": {"django.request": {"level": "ERROR"}},
}
