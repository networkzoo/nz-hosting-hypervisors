#!/usr/bin/env bash
# Creates/updates the dashboard login. Run from the repo root on the VM:  deploy/set-password.sh [username]
set -euo pipefail
USER_NAME=${1:-admin}
read -rsp "Password for $USER_NAME: " PW; echo
read -rsp "Again: " PW2; echo
[[ "$PW" == "$PW2" && -n "$PW" ]] || { echo "Passwords differ or empty" >&2; exit 1; }
HASH=$(docker run --rm caddy:2 caddy hash-password --plaintext "$PW")
mkdir -p caddy
printf 'basicauth {\n\t%s %s\n}\n' "$USER_NAME" "$HASH" > caddy/auth.caddy
chmod 600 caddy/auth.caddy
echo "Wrote caddy/auth.caddy. Apply with: docker compose up -d"
