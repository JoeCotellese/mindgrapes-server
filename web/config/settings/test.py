"""Test settings: sqlite in-memory so unit tests run with no Postgres."""

import os

from .base import *  # noqa: F401, F403

# The Django test client serves over http://testserver, which Authlib rejects
# as insecure transport. Allow it so the OAuth endpoints are exercisable.
os.environ.setdefault("AUTHLIB_INSECURE_TRANSPORT", "1")

DEBUG = False

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# The Playwright e2e ceremony runs over http://localhost (live_server); treat
# that as a secure WebAuthn origin. Inert for the non-browser tests.
MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = True

# Whitenoise serves static at runtime; it is not under test and would warn
# about the (collectstatic-only) STATIC_ROOT. Drop it for pristine output.
MIDDLEWARE = [m for m in MIDDLEWARE if "whitenoise" not in m.lower()]  # noqa: F405
