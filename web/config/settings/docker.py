"""Docker dev settings: inherit local, but use the dev Postgres and mailpit."""

import os

from .local import *  # noqa: F401, F403

# The dev edge serves OAuth over plain http (and via *.orb.local hostnames,
# which are not loopback), so allow Authlib's insecure-transport path in dev.
# Production runs behind TLS and must NOT set this.
os.environ.setdefault("AUTHLIB_INSECURE_TRANSPORT", "1")

# Behind the dev Caddy edge (localhost:8080) and reachable by service DNS.
# OrbStack also exposes containers at <service>.<project>.orb.local.
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "web", ".orb.local"]
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://*.orb.local",
    "https://*.orb.local",
]

# Postgres via DATABASE_URL (the dev stack's postgres service).
DATABASES = {
    "default": env.db("DATABASE_URL"),  # noqa: F405
}

# Email -> mailpit container (Slices 1-2 send enrollment links here).
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="mailpit")  # noqa: F405
EMAIL_PORT = env.int("EMAIL_PORT", default=1025)  # noqa: F405
EMAIL_USE_TLS = False
