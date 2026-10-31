#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
ENV_FILE=${CLOUDKIT_ENV_FILE:-deploy/.env}
if [[ ! -f "$ENV_FILE" ]]; then
  cp deploy/.env.example "$ENV_FILE"
fi
COMPOSE=(docker compose --env-file "$ENV_FILE" -f docker-compose.yml)
command -v docker >/dev/null || { echo 'Docker Engine and Compose v2 are required on the cloud host.' >&2; exit 69; }
run() { "${COMPOSE[@]}" run --rm --no-deps -T worker "$@"; }
case "${1:-status}" in
  build)
    "${COMPOSE[@]}" build worker ;;
  login)
    "${COMPOSE[@]}" build worker
    "${COMPOSE[@]}" run --rm --no-deps init
    "${COMPOSE[@]}" run --rm --no-deps worker codex -c 'cli_auth_credentials_store="file"' login --device-auth ;;
  up)
    "${COMPOSE[@]}" config --quiet
    "${COMPOSE[@]}" build worker
    "${COMPOSE[@]}" run --rm --no-deps init
    "${COMPOSE[@]}" up -d --wait redis
    run prepare
    run probe
    "${COMPOSE[@]}" up -d --wait --wait-timeout 120 worker dispatcher ;;
  ready)
    run probe
    run ready ;;
  status)
    "${COMPOSE[@]}" ps --all ;;
  logs)
    "${COMPOSE[@]}" logs --tail 150 worker dispatcher redis ;;
  stop)
    "${COMPOSE[@]}" stop worker dispatcher redis ;;
  backup)
    output=${2:?Usage: deploy/cloudkit.sh backup /absolute/path/backup.tar.gz}
    [[ "$output" == /* && ! -e "$output" ]] || { echo 'Use a new absolute backup file path.' >&2; exit 64; }
    "${COMPOSE[@]}" stop worker dispatcher redis
    run python /opt/cloudkit/deploy/archive.py backup > "$output.partial"
    mv -- "$output.partial" "$output"
    sha256sum "$output" > "$output.sha256"
    echo "Backup saved: $output ; services remain stopped. Auth must be re-established after recovery." ;;
  restore)
    input=${2:?Usage: deploy/cloudkit.sh restore /absolute/path/backup.tar.gz}
    [[ "$input" == /* && -f "$input.sha256" ]] || { echo 'Absolute archive path and its .sha256 file required.' >&2; exit 64; }
    read -r expected_digest ignored_path < "$input.sha256"
    [[ "$expected_digest" =~ ^[a-fA-F0-9]{64}$ ]] || { echo 'Invalid SHA-256 sidecar.' >&2; exit 65; }
    read -r actual_digest ignored_path < <(sha256sum "$input")
    [[ "${actual_digest,,}" == "${expected_digest,,}" ]] || { echo 'Archive checksum mismatch.' >&2; exit 65; }
    "${COMPOSE[@]}" stop worker dispatcher redis
    "${COMPOSE[@]}" run --rm --no-deps init
    run python /opt/cloudkit/deploy/archive.py restore < "$input"
    echo 'Restored local files. Run login if needed, then up to verify Git/model and rebuild queue delivery.' ;;
  exec)
    shift
    run "$@" ;;
  *) echo 'Usage: deploy/cloudkit.sh {build|login|up|ready|status|logs|stop|backup PATH|restore PATH|exec COMMAND...}' >&2; exit 64 ;;
esac
