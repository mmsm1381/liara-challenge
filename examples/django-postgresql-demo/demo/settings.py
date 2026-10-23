import os
import urllib.parse


def _database_from_env():
    # DB_* variables win when present; otherwise accept one DATABASE_URL.
    if os.environ.get("DB_HOST"):
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["DB_NAME"],
            "USER": os.environ["DB_USER"],
            "PASSWORD": os.environ["DB_PASSWORD"],
            "HOST": os.environ["DB_HOST"],
            "PORT": os.environ.get("DB_PORT", "5432"),
            "CONN_MAX_AGE": 0,
        }
    url = urllib.parse.urlparse(os.environ["DATABASE_URL"])
    if url.scheme not in ("postgres", "postgresql"):
        raise ValueError("DATABASE_URL must use the postgres:// or postgresql:// scheme")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": urllib.parse.unquote(url.path.lstrip("/")),
        "USER": urllib.parse.unquote(url.username or ""),
        "PASSWORD": urllib.parse.unquote(url.password or ""),
        "HOST": url.hostname or "",
        "PORT": str(url.port or 5432),
        "CONN_MAX_AGE": 0,
    }


SECRET_KEY = os.environ["SECRET_KEY"]
DEBUG = False
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
ROOT_URLCONF = "demo.urls"
WSGI_APPLICATION = "demo.wsgi.application"
INSTALLED_APPS = ["bookings"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware", "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware", "django.middleware.clickjacking.XFrameOptionsMiddleware"]
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}]
DATABASES = {"default": _database_from_env()}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
STATIC_URL = "/static/"
STATIC_ROOT = "/tmp/static"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
