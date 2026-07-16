#!/bin/sh
# Apply Django migrations (retrying until Postgres is ready), then run the CMD.
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

exec "$@"
