#!/bin/sh
# Turns the one secret Railway holds (GATE_PASSWORD) into what the Caddyfile
# needs: a bcrypt hash for basic_auth and the cookie value that stands in for
# it afterwards. The cookie is derived, not random, so it survives a restart
# and changing the password logs every browser out.
set -eu

: "${GATE_PASSWORD:?GATE_PASSWORD must be set}"
: "${BACKEND_UPSTREAM:?BACKEND_UPSTREAM must be set, e.g. backend.railway.internal:8000}"
: "${APP_UPSTREAM:?APP_UPSTREAM must be set, e.g. app.railway.internal:8080}"

export GATE_USER="${GATE_USER:-peerkola}"
GATE_PASSWORD_HASH="$(caddy hash-password --plaintext "$GATE_PASSWORD")"
GATE_COOKIE="$(printf 'peerkola-gate:%s:%s' "$GATE_USER" "$GATE_PASSWORD" | sha256sum | cut -d' ' -f1)"
export GATE_PASSWORD_HASH GATE_COOKIE
unset GATE_PASSWORD

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
