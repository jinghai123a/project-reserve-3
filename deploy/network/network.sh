#!/usr/bin/env bash
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMPOSE=(docker compose --project-directory "$HERE" -f "$HERE/compose.yaml")
case "${1:-status}" in
  up)
    "${COMPOSE[@]}" config --quiet
    "${COMPOSE[@]}" up -d --wait --wait-timeout 90
    "${COMPOSE[@]}" exec -T tailscale tailscale ip -4 ;;
  status)
    "${COMPOSE[@]}" ps --all
    "${COMPOSE[@]}" exec -T tailscale tailscale status ;;
  ready)
    "${COMPOSE[@]}" exec -T tailscale wget -q -O /dev/null http://127.0.0.1:9002/healthz
    "${COMPOSE[@]}" exec -T tailscale tailscale serve status
    echo 'Tailnet IP assigned; verify actual SSH access from an authorized peer separately.' ;;
  logs) "${COMPOSE[@]}" logs --tail 80 tailscale ;;
  stop) "${COMPOSE[@]}" stop tailscale ;;
  *) echo 'Usage: deploy/network/network.sh {up|status|ready|logs|stop}' >&2; exit 64 ;;
esac
