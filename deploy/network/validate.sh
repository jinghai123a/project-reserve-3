#!/usr/bin/env bash
# Public CI entry point: static render/assertions with an empty temporary secret.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec bash "$HERE/smoke.sh"
