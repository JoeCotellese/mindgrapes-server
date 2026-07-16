"""Integration-test settings: Postgres `default` with the brain.* schema.

Used by `make dev-test-integration`, which runs inside the dev web container
where PGHOST=postgres reaches the dev Postgres (brain.* loaded from init/*.sql
on the docker-entrypoint mount). Tests connect to the EXISTING dev database
directly — the integration conftest unblocks DB access without creating a
pytest-managed test database — so they must stay read-only or clean up after
themselves.
"""

import os

from .base import *  # noqa: F401, F403

os.environ.setdefault("AUTHLIB_INSECURE_TRANSPORT", "1")

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", default="openbrain_dev"),  # noqa: F405
        "USER": env("POSTGRES_USER", default="openbrain"),  # noqa: F405
        "PASSWORD": env("POSTGRES_PASSWORD", default="devpassword"),  # noqa: F405
        "HOST": env("PGHOST", default="postgres"),  # noqa: F405
        "PORT": env("PGPORT", default="5432"),  # noqa: F405
    }
}

MIDDLEWARE = [m for m in MIDDLEWARE if "whitenoise" not in m.lower()]  # noqa: F405
