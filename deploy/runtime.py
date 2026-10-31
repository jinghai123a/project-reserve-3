#!/usr/bin/env python3
"""Deployment plumbing only; task scheduling remains in CloudKit Git/RQ."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from cloudkit.runtime import codex_worker_backend, continuity, worker_supervisor

DATA = Path('/data')
CONTROL = DATA / 'control'
RECEIPT = DATA / 'readiness.json'


def identity(key):
    value = os.environ.get(key, '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        raise ValueError(key + ' requires owner/repository')
    return value


def deployment_identities():
    """Validate both explicit identities before touching any Git checkout.

    Company bundles use one business repository for both. The underlying runtime
    still supports a deliberately configured, independent control repository.
    """
    return identity('CLOUDKIT_CONTROL_REPOSITORY'), identity('CLOUDKIT_PROJECT_REPOSITORY')


def checked(argv, cwd=None):
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(argv[0] + ' failed; credential-bearing diagnostics suppressed')
    return result.stdout.strip()


def store():
    control, _ = deployment_identities()
    expected = 'https://github.com/' + control + '.git'
    if checked(['git', '-C', str(CONTROL), 'remote', 'get-url', 'origin']) != expected:
        raise ValueError('Control repository identity differs from deployment configuration')
    return continuity.Store(CONTROL)


def validate_project(state):
    if (state.get('control_project_id') != identity('CLOUDKIT_PROJECT_REPOSITORY')
            or state.get('work_branch') != os.environ.get('CLOUDKIT_PROJECT_BRANCH', 'main')):
        raise ValueError('Existing control state belongs to another project/branch; preserved for explicit migration')


def activate_agent_config():
    """Select role configuration independently from the project's state repo."""
    from activate_config import activate
    default = CONTROL / '.codex'
    source = Path(os.environ.get('CLOUDKIT_AGENT_CONFIG_SOURCE') or str(default))
    # Compose injects the default even when the operator did not set the option.
    # Keep that legacy source optional; a custom source must never silently skip.
    if source != default and not (source / 'config.toml').is_file():
        raise ValueError('CLOUDKIT_AGENT_CONFIG_SOURCE must contain config.toml')
    return activate(source, Path(os.environ['CODEX_HOME']))


def prepare():
    control, project = deployment_identities()
    remote = 'https://github.com/' + control + '.git'
    if not (CONTROL / '.git').exists():
        if CONTROL.exists() and any(CONTROL.iterdir()):
            raise ValueError('Existing non-Git control directory preserved; inspect it before retry')
        checked(['git', 'clone', remote, str(CONTROL)])
    if checked(['git', '-C', str(CONTROL), 'status', '--porcelain']):
        raise ValueError('Control working tree has local changes; preserve and review them before updating')
    checked(['git', '-C', str(CONTROL), 'pull', '--ff-only'])
    branch = os.environ.get('CLOUDKIT_PROJECT_BRANCH', 'main')
    checked(['git', 'check-ref-format', '--branch', branch])
    sha = os.environ.get('CLOUDKIT_SOURCE_SHA', '')
    if not sha:
        output = checked(['git', 'ls-remote', 'https://github.com/' + project + '.git', 'refs/heads/' + branch])
        sha = output.split()[0] if output else ''
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('Project branch must exist or CLOUDKIT_SOURCE_SHA must be a full commit SHA')
    checked(['git', 'init', str(DATA / 'probe')])
    if os.environ.get('CLOUDKIT_ACTIVATE_CONTROL_CONFIG', '1') == '1':
        activate_agent_config()
    result = worker_supervisor.init_control(store(), project, sha, branch)
    _, state = store().read()
    validate_project(state)
    return result


def redis_check():
    from redis import Redis
    connection = Redis.from_url(os.environ['CLOUDKIT_REDIS_URL'], socket_connect_timeout=2, socket_timeout=3)
    return bool(connection.ping())


def probe():
    RECEIPT.unlink(missing_ok=True)
    commit, state = store().read()
    validate_project(state)
    redis_check()
    checked(['codex', 'login', 'status'])
    receipt, code = codex_worker_backend.run(
        {'kind': 'capability', 'challenge': uuid.uuid4().hex, 'backend_id': 'container-codex'},
        ['codex'], float(os.environ.get('CLOUDKIT_PROBE_TIMEOUT', '90')),
        cwd=DATA / 'probe', model=os.environ.get('CLOUDKIT_MODEL') or None)
    record = {'passed': code == 0 and receipt['passed'], 'checked_at': time.time(),
              'model': os.environ.get('CLOUDKIT_MODEL', ''), 'control_commit': commit,
              'control_repository': identity('CLOUDKIT_CONTROL_REPOSITORY'),
              'project_repository': identity('CLOUDKIT_PROJECT_REPOSITORY'),
              'project_branch': os.environ.get('CLOUDKIT_PROJECT_BRANCH', 'main'), 'model_receipt': receipt}
    temporary = RECEIPT.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(RECEIPT)
    if not record['passed']:
        raise RuntimeError('Actual model probe failed: ' + receipt.get('failure', 'unproven'))
    return {'passed': True, 'actual_model_response': True, 'control_commit': commit,
            'scope': 'Deployment readiness; business acceptance remains independent'}


def ready():
    receipt = json.loads(RECEIPT.read_text())
    if (receipt.get('passed') is not True or not receipt.get('model_receipt', {}).get('model_output_received')
            or receipt.get('model') != os.environ.get('CLOUDKIT_MODEL', '')
            or receipt.get('control_repository') != identity('CLOUDKIT_CONTROL_REPOSITORY')
            or receipt.get('project_repository') != identity('CLOUDKIT_PROJECT_REPOSITORY')
            or receipt.get('project_branch') != os.environ.get('CLOUDKIT_PROJECT_BRANCH', 'main')):
        raise ValueError('Readiness proof is missing, failed, or belongs to different configuration')
    # Startup proof expires; a live worker still probes capabilities on each task.
    if not 0 <= time.time() - receipt['checked_at'] <= 86400:
        raise ValueError('Readiness proof expired; run deploy/cloudkit.sh ready')
    checked(['codex', 'login', 'status'])
    _, state = store().read()
    validate_project(state)
    redis_check()
    return {'passed': True, 'scope': 'Recent model proof, current auth, Git and Redis access'}


def health():
    # Lifecycle check deliberately makes no model request or Git write.
    active = any(b'cloudkit.runtime.queue_bridge' in p.read_bytes()
                 for p in Path('/proc').glob('[0-9]*/cmdline') if p.exists())
    if not active or not redis_check():
        raise ValueError('Queue consumer process or Redis unavailable')
    return {'passed': True, 'scope': 'Consumer lifecycle, not model or task acceptance'}


def dispatcher_health():
    age = time.time() - float((DATA / 'dispatcher-heartbeat').read_text())
    if not 0 <= age < 180 or not redis_check():
        raise ValueError('Dispatcher heartbeat is stale or Redis unavailable')
    return {'passed': True, 'scope': 'Due-outbox reconciliation lifecycle'}


def worker():
    ready()
    argv = [sys.executable, '-m', 'cloudkit.runtime.queue_bridge', '--repository', str(CONTROL),
            '--queue', os.environ.get('CLOUDKIT_QUEUE', 'cloudkit'),
            '--github-repository', identity('CLOUDKIT_CONTROL_REPOSITORY'), 'worker', '--poll']
    os.execv(sys.executable, argv)


if __name__ == '__main__':
    try:
        action = sys.argv[1]
        if action not in {'prepare', 'probe', 'ready', 'health', 'dispatcher_health', 'worker'}:
            raise ValueError('Unknown deployment operation')
        print(json.dumps(globals()[action](), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'passed': False, 'error_type': type(error).__name__,
                          'error': codex_worker_backend.scrub(str(error))}, ensure_ascii=False))
        sys.exit(1)
