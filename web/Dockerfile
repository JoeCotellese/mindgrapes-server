# ABOUTME: Production image for the Mind Grapes Django app (local-prod over Tailscale).
# ABOUTME: uv-installed deps (no dev extras), source copied in, uvicorn workers, no reload.
#
# Differs from Dockerfile.dev: installs WITHOUT the dev extra, COPYs the app
# source in (no bind mount in prod), defaults to config.settings.production, and
# the entrypoint runs collectstatic in addition to migrate.

FROM python:3.12-slim

# uv for dependency installation.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

WORKDIR /app

# Install deps first for layer caching. Runtime only — no dev extras.
COPY pyproject.toml uv.lock ./
RUN uv pip install --system --no-cache -r pyproject.toml

# App source (prod is not bind-mounted; .dockerignore keeps .venv/caches out).
COPY . /app

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && useradd -m -u 1000 appuser \
 && mkdir -p /app/staticfiles \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENTRYPOINT ["entrypoint.sh"]
CMD ["uvicorn", "config.asgi:application", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "3"]
