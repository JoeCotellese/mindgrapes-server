"""Base Django settings shared across all environments.

Environment-specific overrides live in local.py, docker.py, production.py,
and test.py. The Django app owns its own tables in the public schema of the
shared Postgres, and since the Brain UI migration (#101) it also reads and
writes the brain.* schema directly (re-embedding edited content via OpenRouter).
"""

from pathlib import Path

import environ

# web/ — the Django project root (settings file is config/settings/base.py).
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="django-insecure-change-me-in-production")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")


# Application definition

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",  # allauth templates use humanize filters
]

THIRD_PARTY_APPS = [
    "django_htmx",
    "allauth",
    "allauth.account",
    "allauth.mfa",
]

LOCAL_APPS = [
    "openbrain.accounts",
    "openbrain.brain",
    "openbrain.core",
    "openbrain.mcp",
    "openbrain.oauth",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "openbrain" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "openbrain.brain.context_processors.review_badge",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# Database. One Postgres, two schemas: Django tables live in public; brain.*
# is defined by the init/ SQL and accessed via the raw-SQL seam in
# openbrain/brain/db.py. Defaults to sqlite so unit tests
# and a bare `manage.py` run need no Postgres.
DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}

# DB-backed sessions (no Redis in v1).
SESSION_ENGINE = "django.contrib.sessions.backends.db"


# Custom user model — email identity, no username. Established now because
# AUTH_USER_MODEL cannot be changed cleanly after migrations exist.
AUTH_USER_MODEL = "accounts.User"

# No password validators wired: v1 auth is passwordless passkeys (Slice 1).
AUTH_PASSWORD_VALIDATORS = []


# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files — served by whitenoise so they work under uvicorn (no runserver).
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "openbrain" / "static"]
WHITENOISE_USE_FINDERS = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# django-allauth — installed and configured, but no flows wired in Slice 0.
# Passwordless passkey enrollment/login lands in Slice 1 (#63).
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_VERIFICATION = "none"

LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/"

# Passwordless passkeys (Slice 1, #63). WebAuthn + recovery codes only — no
# password, no TOTP. Passkey login is on; self-serve signup is off because
# users are provisioned by an admin (Slice 2). Enrollment registers a
# discoverable, user-verifying passkey via a custom view (EnrollPasskeyView),
# since the stock add view registers non-discoverable second-factor keys.
MFA_SUPPORTED_TYPES = ["webauthn", "recovery_codes"]
MFA_PASSKEY_LOGIN_ENABLED = True
MFA_PASSKEY_SIGNUP_ENABLED = False

ACCOUNT_ADAPTER = "openbrain.accounts.adapter.OpenBrainAccountAdapter"
MFA_ADAPTER = "openbrain.accounts.adapter.OpenBrainMFAAdapter"

# Sender for enrollment-link emails (Slice 2, #64). The per-environment EMAIL
# backend (console/mailpit/smtp/locmem) decides where it actually goes.
DEFAULT_FROM_EMAIL = env(
    "DEFAULT_FROM_EMAIL", default="Mind Grapes <noreply@openbrain.local>"
)


# OAuth 2.1 authorization server (Slice 3, #73+). Django signs short-lived
# EdDSA access tokens; the MCP resource server (run_mcp) validates them offline
# via the published JWKS. OAUTH_ISSUER/OAUTH_AUDIENCE are shared with that
# service (same .env), keeping signer and validator in lockstep.
OAUTH_ISSUER = env("OAUTH_ISSUER", default="http://localhost:8080")
OAUTH_AUDIENCE = env("OAUTH_AUDIENCE", default="brain")
OAUTH_ACCESS_TTL_SECONDS = env.int("OAUTH_ACCESS_TTL_SECONDS", default=600)
# Ed25519 private key (PKCS8 PEM) used to sign access tokens. Blank in unit
# tests (which inject their own); provision in dev via `manage.py gen_jwt_key`.
OAUTH_JWT_PRIVATE_KEY = env("OAUTH_JWT_PRIVATE_KEY", default="")

# Authlib authorization server (Slice 3.2, #74). The access-token generator is
# our JWT signer, so issued access tokens are the EdDSA JWTs the MCP resource
# server validates offline. Refresh tokens are opaque, rotating strings. The
# token-response expires_in is pinned to the JWT TTL so clients refresh in step
# with the actual token lifetime.
AUTHLIB_OAUTH2_PROVIDER = {
    "access_token_generator": "openbrain.oauth.server.generate_jwt_access_token",
    "refresh_token_generator": True,
    "token_expires_in": {
        "authorization_code": OAUTH_ACCESS_TTL_SECONDS,
        "refresh_token": OAUTH_ACCESS_TTL_SECONDS,
    },
}

# The MCP endpoint a member pastes into Claude to connect, shown on /connect
# (Slice 3.4, #76). Environment-aware: the dev default below; prod sets the
# public https URL (https://brain.example.net/mcp) via the BRAIN_MCP_URL env
# var. The public value depends on #58 (TLS).
BRAIN_MCP_URL = env("BRAIN_MCP_URL", default="http://localhost:8080/mcp")

# Python MCP server (epic #117). run_mcp validates Django's EdDSA tokens offline
# against this JWKS (the in-cluster web URL, distinct from the public issuer) and
# serves the MCP endpoint at MCP_PATH.
OAUTH_JWKS_URL = env("OAUTH_JWKS_URL", default="http://web:8000/oauth/jwks.json")
MCP_PATH = env("MCP_PATH", default="/mcp/")

# Brain UI (Epic #35, #101). The web app reads + writes the brain.* schema and
# re-embeds edited content via OpenRouter, using the same embedding path as the
# MCP server. The key is shared through the same .env. Blank in unit tests
# (which stub the embedding client).
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY", default="")

# Search embeds the query through this function (a dotted path resolved at call
# time). Indirected as a seam so tests stub embeddings without calling
# OpenRouter; production resolves to the real client.
BRAIN_EMBED_FN = "openbrain.brain.embeddings.get_embedding"

# Bare capture_thought extracts metadata through this function (a dotted path
# resolved at call time), indirected as a seam like BRAIN_EMBED_FN so tests stub
# the LLM call; production resolves to the real gpt-4o-mini extractor.
BRAIN_METADATA_FN = "openbrain.brain.extraction.metadata.extract_metadata"

# capture_thought stamps the authenticated member as owner; under the legacy key
# (no sub) it falls back to BRAIN_DEFAULT_OWNER. account_id defaults to the
# household. DEFAULT_OWNER / HOUSEHOLD_ACCOUNT_ID come from the shared .env.
BRAIN_DEFAULT_OWNER = env("DEFAULT_OWNER", default="owner")
BRAIN_HOUSEHOLD_ACCOUNT_ID = env("HOUSEHOLD_ACCOUNT_ID", default="household")

# capture_image (#42). Image blobs live in S3-compatible object storage; the
# vision fallback describes an otherwise-textless image. Both are seams:
# BLOBSTORE_BACKEND selects the in-memory fake (default, and what the unit suite
# uses) vs the real S3 client; BRAIN_VISION_FN is a dotted path resolved at call
# time so tests never egress image bytes. Production/dev-stack set backend='s3'
# and the S3_* vars from the shared .env — never derived from a request or EXIF.
BLOBSTORE_BACKEND = env("BLOBSTORE_BACKEND", default="memory")
S3_ENDPOINT = env("S3_ENDPOINT", default="")
# The address a CLIENT fetches presigned URLs from (the tailnet host), as opposed
# to S3_ENDPOINT which is the compose-network address the server puts/gets
# through. SigV4 signs the Host header, so a URL minted against the internal
# endpoint 403s when fetched at the public one. Blank = same address for both.
S3_PUBLIC_ENDPOINT = env("S3_PUBLIC_ENDPOINT", default="")
S3_BUCKET = env("S3_BUCKET", default="brain-attachments")
S3_ACCESS_KEY = env("S3_ACCESS_KEY", default="")
S3_SECRET_KEY = env("S3_SECRET_KEY", default="")
S3_REGION = env("S3_REGION", default="")
BRAIN_VISION_FN = "openbrain.brain.vision.describe_image"

# Hard ceiling on a multipart photo POSTed to /capture/image (#42 app path).
# Enforced BEFORE any decode so an oversize body never reaches Pillow. Sized for
# a full-resolution phone photo (HEIC/JPEG); the MCP base64 door stays far
# smaller (image_captures.MAX_BASE64_CHARS).
MAX_IMAGE_UPLOAD_BYTES = env.int("MAX_IMAGE_UPLOAD_BYTES", default=12 * 1024 * 1024)
