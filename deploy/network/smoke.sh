#!/usr/bin/env bash
# Static validation only: does not pull/start containers or authenticate.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TEMP=$(mktemp -d)
trap 'rm -rf -- "$TEMP"' EXIT
export CLOUDKIT_TAILSCALE_AUTHKEY_FILE="$TEMP/empty-key"
: > "$CLOUDKIT_TAILSCALE_AUTHKEY_FILE"
bash -n "$HERE/network.sh" "$HERE/smoke.sh"
sh -n "$HERE/config/entrypoint.sh"
docker compose --project-directory "$HERE" -f "$HERE/compose.yaml" config --format json \
  | python3 "$HERE/validate.py"
