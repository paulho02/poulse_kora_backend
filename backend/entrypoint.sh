#!/bin/sh
# Production/CI entrypoint: always bring the schema up to date before serving
# traffic. Local dev bypasses this via docker-compose.override.yml, which sets
# its own `command:` (uvicorn --reload) and never calls this script.
set -e

alembic upgrade head

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
