#!/bin/sh
# ABOUTME: Production entrypoint for the Django web image.
# ABOUTME: Applies migrations (retry until Postgres ready), collects static, execs CMD.
set -e

n=0
until python manage.py migrate --noinput; do
    n=$((n + 1))
    if [ "$n" -ge 15 ]; then
        echo "migrate failed after $n attempts; giving up" >&2
        exit 1
    fi
    echo "migrate failed (Postgres not ready?); retry $n in 2s..." >&2
    sleep 2
done

# whitenoise serves static under uvicorn; collect once at boot.
python manage.py collectstatic --noinput

exec "$@"
