#!/usr/bin/env bash
# Run as root on ONE node of each Proxmox cluster (users, ACLs and tokens are cluster-wide).
# Creates a read-only monitoring user + API token for the dashboard, then prints the config to paste.
#
#   ./pve-create-monitor.sh [cluster-name] [--rotate]
#
# Safe to re-run: existing user/permissions are left alone. If the token already exists its secret
# can't be shown again; use --rotate to replace it with a new one.
# Needs only the built-in PVEAuditor role. (Not included: the optional "pending updates" count,
# which needs Sys.Modify and is more than read-only.)
set -euo pipefail

USER_ID=${MONITOR_USER:-monitor@pve}
TOKEN=${MONITOR_TOKEN:-dashboard}
ROLE=${MONITOR_ROLE:-PVEAuditor}
ROTATE=0
NAME=""
for a in "$@"; do
  case $a in
    --rotate) ROTATE=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) NAME=$a ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run as root on a Proxmox node"
command -v pveum >/dev/null && command -v pvesh >/dev/null || die "pveum/pvesh not found - is this a Proxmox VE node?"

if [[ -z $NAME ]]; then
  NAME=$(pvecm status 2>/dev/null | awk '/^Name:/{print $2; exit}') || true
  NAME=${NAME:-$(hostname -s)}
fi
ENVVAR="PVE_$(tr -c 'A-Za-z0-9\n' '_' <<<"${NAME^^}")_TOKEN"
TOKEN_ID="$USER_ID!$TOKEN"

echo ">> User $USER_ID"
if pvesh get "/access/users/$USER_ID" >/dev/null 2>&1; then
  echo "   already exists"
else
  pveum user add "$USER_ID" --comment "Read-only monitoring for the fleet dashboard"
  echo "   created (no password; it can't log in)"
fi

echo ">> Permission: $ROLE on / (read-only)"
pveum acl modify / --users "$USER_ID" --roles "$ROLE"

echo ">> API token $TOKEN_ID"
if pvesh get "/access/users/$USER_ID/token/$TOKEN" >/dev/null 2>&1; then
  if [[ $ROTATE -eq 1 ]]; then
    pveum user token remove "$USER_ID" "$TOKEN"
    echo "   old token removed"
  else
    echo "   already exists - its secret can't be shown again."
    echo "   Re-run with --rotate to replace it with a new one."
    exit 0
  fi
fi
OUT=$(pveum user token add "$USER_ID" "$TOKEN" --privsep 0 --comment "Fleet dashboard" --output-format json)
SECRET=$(grep -oP '"value"\s*:\s*"\K[^"]+' <<<"$OUT" || true)
[[ -n $SECRET ]] || die "token created but secret not found in output; delete it and retry"

echo ">> Testing the token against this node"
if curl -sk -K - https://localhost:8006/api2/json/version <<<"header = \"Authorization: PVEAPIToken=$TOKEN_ID=$SECRET\"" | grep -q '"version"'; then
  echo "   OK"
else
  echo "   WARNING: test request failed (is port 8006 reachable from here?). Token may still be fine." >&2
fi

IP=$(hostname -I | awk '{print $1}')
cat <<MSG

=============================================================================
 SAVE THIS NOW - the secret is shown only once. Nothing was written to disk.
=============================================================================
 Add to the dashboard's .env  (edit with nano; avoid pasting into shell history):
   $ENVVAR=$SECRET

 Add to config.yaml  (check the url is the address the dashboard VM should use):
   - name: $NAME
     type: proxmox
     url: https://$IP:8006
     token_id: $TOKEN_ID
     token_secret_env: $ENVVAR
     verify_tls: false
=============================================================================
MSG
