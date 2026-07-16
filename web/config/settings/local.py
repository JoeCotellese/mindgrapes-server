"""Local (host) development settings: debug on, sqlite, relaxed security."""

from .base import *  # noqa: F401, F403

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]

# WebAuthn (Slice 1) treats localhost as a secure context; allow insecure
# origin so http://localhost works during development.
MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = True

# Mailpit if running locally; otherwise the console.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
