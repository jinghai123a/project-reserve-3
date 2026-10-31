#!/usr/bin/env bash
# Isolated container integration test. Never calls a model or private repository.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
TEMP=$(mktemp -d)
export COMPOSE_PROJECT_NAME="cloudkit-smoke-$$"
export CLOUDKIT_IMAGE_TAG="smoke-$$"
# Required deployment identities are explicit fixture values; this smoke never clones them.
export CLOUDKIT_CONTROL_REPOSITORY=fixture/offline-project
export CLOUDKIT_PROJECT_REPOSITORY=fixture/offline-project
export CLOUDKIT_GITHUB_TOKEN_FILE="$TEMP/github_token"
export CLOUDKIT_AGENT_CONFIG_SOURCE=/opt/cloudkit/deploy/company-rules/.codex
printf '\n' > "$CLOUDKIT_GITHUB_TOKEN_FILE"
chmod 644 "$CLOUDKIT_GITHUB_TOKEN_FILE"
COMPOSE=(docker compose -f docker-compose.yml)
cleanup() {
  "${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker image rm "codexcloudkit:${CLOUDKIT_IMAGE_TAG}" >/dev/null 2>&1 || true
  rm -rf -- "$TEMP"
}
trap cleanup EXIT
prepare_context() {
  local context="$1"
  # Keep broad credential exclusions, then add only the public image inputs.
  mkdir -p "$context"
  tar --exclude='.git' --exclude='.codex' --exclude='.env' --exclude='.env.*' \
    --exclude='secrets' --exclude='backups' --exclude='__pycache__' \
    --exclude='.venv' --exclude='node_modules' --exclude='*.log' \
    -cf - Dockerfile docker-compose.yml .dockerignore pyproject.toml README.md LICENSE src deploy \
    | tar -xf - -C "$context"
  mkdir -p "$context/.codex/agents" "$context/company" "$context/docs"
  cp .codex/config.toml "$context/.codex/config.toml"
  cp .codex/agents/*.toml "$context/.codex/agents/"
  cp company/organization.json "$context/company/organization.json"
  cp docs/WORKFLOW.zh-CN.md "$context/docs/WORKFLOW.zh-CN.md"
  # Harmless canaries check exclusions for root roles, legacy bundled rules,
  # and unrelated .codex directories. No real credentials enter this context.
  mkdir -p "$context/deploy/company-rules/.codex" "$context/deploy/smoke-excluded/.codex"
  printf 'SMOKE_EXCLUDED_FILE\n' > "$context/.codex/auth.json"
  printf 'SMOKE_EXCLUDED_FILE\n' > "$context/.codex/.env"
  printf 'SMOKE_EXCLUDED_FILE\n' > "$context/deploy/company-rules/.codex/auth.json"
  printf 'SMOKE_EXCLUDED_FILE\n' > "$context/deploy/company-rules/.codex/.env"
  printf 'SMOKE_EXCLUDED_FILE\n' > "$context/deploy/smoke-excluded/.codex/marker"
  chmod 755 "$context/.codex" "$context/.codex/agents" "$context/company" "$context/docs" \
    "$context/deploy/company-rules" "$context/deploy/company-rules/.codex" \
    "$context/deploy/smoke-excluded" "$context/deploy/smoke-excluded/.codex"
  chmod 644 "$context/.codex/config.toml" "$context/.codex/agents/"*.toml \
    "$context/company/organization.json" "$context/docs/WORKFLOW.zh-CN.md"
}
# Build a disposable context, never add fixtures or copy credentials to the repo.
BUILD_CONTEXT="$TEMP/context"
prepare_context "$BUILD_CONTEXT"
cd "$BUILD_CONTEXT"
"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" build worker
"${COMPOSE[@]}" run --rm --no-deps init
"${COMPOSE[@]}" up -d --wait --wait-timeout 90 redis
run() { "${COMPOSE[@]}" run --rm --no-deps -T worker "$@"; }
run python -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version; print(sys.version)'
run node --version
run npm --version
run codex --version
run python -c 'from pathlib import Path; import os; assert os.getuid() == 1000; p=Path("/data/probe/permissions"); p.write_text("ok"); p.unlink()'
run python -c 'import os; from redis import Redis; assert Redis.from_url(os.environ["CLOUDKIT_REDIS_URL"]).ping()'
run python - <<'PY'
import os
import json
from pathlib import Path
import sys
import tempfile
import tomllib

sys.path.insert(0, '/opt/cloudkit/deploy')
from runtime import activate_agent_config

source = Path(os.environ['CLOUDKIT_AGENT_CONFIG_SOURCE'])
config = source / 'config.toml'
roles = {key: value for key, value in tomllib.loads(config.read_text())['agents'].items()
         if isinstance(value, dict)}
organization = json.loads(Path('/opt/cloudkit/deploy/company-rules/company/organization.json').read_text())
assert len(roles) == 28 and set(roles) == set(organization['roles'])
assert Path('/opt/cloudkit/docs/WORKFLOW.zh-CN.md').is_file()
for settings in roles.values():
    role = source / settings['config_file']
    assert '方案驱动与连续推进' in tomllib.loads(role.read_text())['developer_instructions']
assert not (source / 'auth.json').exists() and not (source / '.env').exists()
assert not Path('/opt/cloudkit/deploy/smoke-excluded/.codex').exists()
login = Path(os.environ['CODEX_HOME']) / 'auth.json'
assert not login.exists()
with tempfile.TemporaryDirectory(prefix='cloudkit-role-smoke-') as directory:
    os.environ['CODEX_HOME'] = directory
    result = activate_agent_config()
    assert result['activated'] and result['authentication_preserved'] and result['files'] == 29, result
    assert (Path(directory) / 'config.toml').read_bytes() == config.read_bytes()
    for settings in roles.values():
        relative = settings['config_file']
        assert (Path(directory) / relative).read_bytes() == (source / relative).read_bytes()
    assert not (Path(directory) / 'auth.json').exists()
assert not login.exists()
print('PASS: packaged company roles readable and activated in isolated CODEX_HOME')
PY
if run ready; then
  echo 'ERROR: readiness passed without a real model receipt' >&2
  exit 1
fi
# Backup/restore exercises filesystem artifacts with no controller/model call.
run python -c 'from pathlib import Path; p=Path("/data/runs/smoke"); p.mkdir(); (p/"result.txt").write_text("preserved")'
run python /opt/cloudkit/deploy/archive.py backup > "$TEMP/backup.tar.gz"
run python -c 'import shutil; shutil.rmtree("/data/runs/smoke")'
run python /opt/cloudkit/deploy/archive.py restore < "$TEMP/backup.tar.gz"
run python -c 'from pathlib import Path; assert Path("/data/runs/smoke/result.txt").read_text() == "preserved"; assert not Path("/data/home/.codex/auth.json").exists()'
echo 'PASS: image, packaged company roles, isolated role activation, non-root volumes, Redis, missing-auth refusal, backup/restore. No model or cloud production deployment was tested.'
