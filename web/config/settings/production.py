"""Production settings: strict security, all secrets from the environment."""

from .base import *  # noqa: F401, F403

DEBUG = False

SECRET_KEY = env("SECRET_KEY")  # noqa: F405
# A missing key raises above; an empty or example-file key must fail just as
# loudly, or a self-hoster ships with a value that's public on GitHub.
if not SECRET_KEY or SECRET_KEY.startswith("django-insecure-"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        "SECRET_KEY is empty or a django-insecure-* placeholder; generate one with "
        "python -c \"import secrets; print(secrets.token_urlsafe(64))\""
    )
# Loopback is always allowed so the in-container healthcheck — which probes
# http://127.0.0.1:8000/healthz directly, with no Host rewrite — is never
# rejected with DisallowedHost. External hostnames still come from the env var;
# dict.fromkeys dedupes when the operator already lists loopback (the .env.example
# value does).
ALLOWED_HOSTS = list(
    dict.fromkeys(env("ALLOWED_HOSTS") + ["127.0.0.1", "localhost"])  # noqa: F405
)
# Origins trusted for unsafe (POST) requests — the OAuth authorize form and
# passkey enrollment POST over HTTPS, so the serving origin must be listed
# (e.g. https://brain.example.com). Comma-separated in .env.
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])  # noqa: F405

# Build the connection from discrete vars rather than a DATABASE_URL so a
# password with URL-significant characters (@ : / ?) needs no percent-encoding —
# the live POSTGRES_PASSWORD is a strong random string. Mirrors how the mcp
# service is wired (PGHOST/PGUSER/...). Same shared Postgres, public.* only.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB"),  # noqa: F405
        "USER": env("POSTGRES_USER"),  # noqa: F405
        "PASSWORD": env("POSTGRES_PASSWORD"),  # noqa: F405
        "HOST": env("PGHOST", default="postgres"),  # noqa: F405
        "PORT": env("PGPORT", default="5432"),  # noqa: F405
    }
}

# Behind the Caddy edge, which terminates TLS and forwards plain HTTP.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)  # noqa: F405
# The container healthcheck probes http://127.0.0.1:8000/healthz directly (no
# Caddy hop, so no X-Forwarded-Proto), which SECURE_SSL_REDIRECT would 301 to
# https on a plaintext port — the probe then fails on the TLS-on-cleartext hop.
# Exempt the liveness path; real external traffic still arrives as https (forced
# at the edge) and is unaffected by the exemption.
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)  # noqa: F405
SECURE_HSTS_INCLUDE_SUBDOMAINS = bool(SECURE_HSTS_SECONDS)
SECURE_HSTS_PRELOAD = bool(SECURE_HSTS_SECONDS)

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="")  # noqa: F405
EMAIL_PORT = env.int("EMAIL_PORT", default=587)  # noqa: F405
EMAIL_USE_TLS = True
