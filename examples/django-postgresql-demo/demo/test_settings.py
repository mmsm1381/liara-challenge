"""Local model/view tests; PostgreSQL compatibility is a separate Compose check."""
import os

for key in ("SECRET_KEY", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST"):
    os.environ.setdefault(key, "tests-only")
from .settings import *  # noqa: F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
