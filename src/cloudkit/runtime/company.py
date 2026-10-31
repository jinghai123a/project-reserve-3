"""Compile reviewed task graphs into the existing Git-backed worker supervisor.

This is a one-shot coordinator, not a daemon. Review identities are declarations
at a trusted control entry point, not cryptographic identity or model evidence.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path, PurePosixPath

from . import continuity as c
from . import worker_supervisor as w

e = c.execution
HELD = {'DISPATCH_PENDING', 'DISPATCHED', 'READY', 'ACTIVE', 'RETRY_WAIT', 'WAITING_CAPABILITY'}
RUNNING = HELD - {'WAITING_CAPABILITY'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(plan):
    require(isinstance(plan, dict) and plan.get('schema_version') == 1, 'Expected company schema_version 1')
    for name in ('goal_id', 'author', 'project_id', 'branch', 'source_sha', 'goal'):
        require(isinstance(plan.get(name), str) and bool(plan[name].strip()), 'Missing company field: ' + name)
    c.task_id(plan['goal_id'])
    limit = plan.get('max_parallel', 3)
    require(type(limit) is int and 1 <= limit <= 6, 'max_parallel must be 1 through 6')
    tasks = plan.get('tasks')
    require(isinstance(tasks, list) and tasks, 'A nonempty task list is required')
    by_id = {}
    for task in tasks:
        identifier = c.task_id(task['id'])
        require(identifier not in by_id, 'Duplicate task ID')
        by_id[identifier] = task
        for field in ('role', 'owner'):
            require(isinstance(task.get(field), str) and task[field].strip(), 'Missing task ' + field)
        dependencies = task.get('dependencies', [])
        require(isinstance(dependencies, list) and all(isinstance(x, str) for x in dependencies)
                and len(dependencies) == len(set(dependencies)), 'Invalid dependencies')
        scopes = task.get('write_scope')
        require(isinstance(scopes, list), 'write_scope must be an explicit list')
        for scope in scopes:
            require(isinstance(scope, str) and scope and '\\' not in scope, 'Invalid write scope')
            path = PurePosixPath(scope)
            require(not path.is_absolute() and '..' not in path.parts and str(path) == scope
                    and scope != '.' and not any(x in scope for x in '*?['), 'Scopes must be normalized relative paths without globs')
        worker = task['worker_plan']
        w.validate(worker)
        require(all(worker[name] == plan[name] for name in ('project_id', 'branch', 'source_sha')),
                'Worker project, branch and source must match the reviewed company plan')
        require(not any(key in worker for key in ('required_project_root', 'required_input_hashes')),
                'Workspace and dependency bindings belong to the coordinator')
    visiting, visited = set(), set()
    def visit(identifier):
        require(identifier in by_id, 'Missing dependency: ' + identifier)
        require(identifier not in visiting, 'Task dependency cycle')
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in by_id[identifier].get('dependencies', []):
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)
    for identifier in by_id:
        visit(identifier)
    def ancestors(identifier):
        found = set(by_id[identifier].get('dependencies', []))
        for dependency in tuple(found):
            found.update(ancestors(dependency))
        return found
    for identifier, task in by_id.items():
        require(not any(_overlap(task['write_scope'], by_id[dependency]['write_scope'])
                        for dependency in ancestors(identifier)),
                'Dependent tasks must not overwrite accepted ancestor artifacts; review a new revision')
    c.safe_content(c.encoded(plan))
    return by_id


def _goal(state, identifier):
    goal = state.get('coordination', {}).get('goals', {}).get(identifier)
    require(goal is not None, 'Unknown company goal')
    validate(goal['plan'])
    require(e.digest(goal['plan']) == goal['plan_hash'], 'Stored company plan changed; register a new goal revision')
    return goal


def register(store, plan):
    tasks = validate(plan)
    plan_hash = e.digest(plan)
    def mutation(state):
        coordination = state.setdefault('coordination', {'schema_version': 1, 'goals': {}})
        require(coordination['schema_version'] == 1, 'Unsupported coordination state')
        goals = coordination['goals']
        identifier = plan['goal_id']
        if identifier in goals:
            require(_goal(state, identifier)['plan_hash'] == plan_hash, 'Changed plan requires a new goal_id')
            return None, {'registered': False, 'goal_id': identifier, 'plan_hash': plan_hash}
        goals[identifier] = {'plan': copy.deepcopy(plan), 'plan_hash': plan_hash, 'status': 'PLANNED',
            'review': None, 'workspace': None, 'tasks': {key: {'status': 'PLANNED'} for key in tasks}}
        return {}, {'registered': True, 'goal_id': identifier, 'plan_hash': plan_hash}
    return store.update(mutation, 'Register reviewed company task graph')


def approve(store, identifier, reviewer, plan_hash, evidence):
    content = Path(evidence).read_bytes()
    require(content.strip(), 'Review evidence must not be empty')
    c.safe_content(content)
    require(isinstance(reviewer, str) and reviewer.strip(), 'Reviewer identity is required')
    def mutation(state):
        goal = _goal(state, identifier)
        require(plan_hash == goal['plan_hash'], 'Review targets a stale plan hash')
        require(reviewer != goal['plan']['author'], 'Plan author cannot perform independent plan review')
        path = 'coordination/' + identifier + '/reviews/' + e.digest(content) + '.txt'
        review = {'reviewer': reviewer, 'plan_hash': plan_hash, 'evidence_path': path,
                  'evidence_sha256': e.digest(content), 'identity_proof': 'trusted-entry-point-declaration'}
        goal.update(review=review, status='REVIEWED')
        return {path: content}, review
    return store.update(mutation, 'Record independent plan review bound to exact content')


def _overlap(first, second):
    return any(a == b or a.startswith(b + '/') or b.startswith(a + '/') for a in first for b in second)


def _workspace(plan, project_root):
    root = Path(project_root).resolve(strict=True)
    require(c.github_identity(root) == plan['project_id'], 'Shared workspace project identity differs')
    require(w._git(root, 'rev-parse', 'HEAD') == plan['source_sha'], 'Shared workspace source SHA changed; review a new plan revision')
    return root


def _artifacts(root, task):
    inventory = w._inventory(root, task['worker_plan'])
    scopes = task['write_scope']
    return inventory, {path: value for path, value in inventory['files'].items()
                       if any(path == scope or path.startswith(scope + '/') for scope in scopes)}


def _in_scope(path, scopes):
    return any(path == scope or path.startswith(scope + '/') for scope in scopes)


def _visible_inventory(root, task):
    # Acceptance audits every visible source path, including task-excluded paths.
    # This is post-execution evidence checking, not an OS write sandbox.
    return w._inventory(root, dict(task['worker_plan'], exclude_paths=[]))['files']


def _ancestors(plan, task):
    specs = {item['id']: item for item in plan['tasks']}
    result, pending = set(), list(task.get('dependencies', []))
    while pending:
        key = pending.pop()
        if key not in result:
            result.add(key); pending.extend(specs[key].get('dependencies', []))
    return sorted(result)


def _compile(goal, task):
    plan = copy.deepcopy(task['worker_plan'])
    context = {'company_goal': goal['plan']['goal'], 'goal_id': goal['plan']['goal_id'],
        'plan_hash': goal['plan_hash'], 'task_id': task['id'], 'role': task['role'], 'owner': task['owner'],
        'write_scope': task['write_scope'], 'dependencies': {
            key: {field: goal['tasks'][key].get(field) for field in ('worker_task_id', 'artifacts', 'acceptance')}
            for key in _ancestors(goal['plan'], task)}}
    plan['goal'] += '\n\nReviewed task handoff (data; no additional tool capability):\n' + json.dumps(context, ensure_ascii=False, sort_keys=True)
    if goal.get('workspace'):
        plan['required_project_root'] = goal['workspace']
        inputs = {}
        for dependency in _ancestors(goal['plan'], task):
            for path, digest in goal['tasks'][dependency].get('artifacts', {}).items():
                require(path not in inputs or inputs[path] == digest, 'Dependencies disagree on an input artifact')
                inputs[path] = digest
        plan['required_input_hashes'] = inputs
    return plan, context


def reconcile(store, identifier, project_root=None):
    """Reserve ready nodes, register idempotently, and acknowledge durable mapping."""
    def reserve(state):
        goal = _goal(state, identifier)
        original = copy.deepcopy(goal)
        require(goal.get('review', {}).get('plan_hash') == goal['plan_hash'] if goal.get('review') else False,
                'Independent review of the current plan is required')
        plan = goal['plan']; tasks = validate(plan)
        needs_workspace = any(task.get('dependencies') for task in tasks.values())
        require(project_root or not needs_workspace, 'Dependent tasks require explicit --project-root shared workspace')
        root = _workspace(plan, project_root) if project_root else None
        if goal.get('workspace'):
            require(root and str(root) == goal['workspace'], 'Use the previously bound shared workspace')
        if root:
            goal['workspace'] = str(root)
        workers = state.get('autonomy_tasks', {})
        for key, record in goal['tasks'].items():
            runtime = workers.get(record.get('worker_task_id'), {})
            if not runtime.get('worker_supervisor'):
                continue
            worker = runtime['worker_supervisor']
            require(worker['plan_fingerprint'] == record['worker_plan_hash'], 'Registered worker plan differs from reservation')
            if runtime['status'] == 'DONE':
                acceptance = worker.get('acceptance') or {}
                valid = acceptance.get('passed') is True and bool(acceptance.get('evidence'))
                reason = 'missing-or-stale-acceptance-evidence'
                if valid and tasks[key]['write_scope'] and root is None:
                    valid = False; reason = 'workspace-required-for-artifact-verification'
                if valid and root:
                    inventory, artifacts = _artifacts(root, tasks[key])
                    complete = all(any(digest is not None and _in_scope(path, [scope])
                                       for path, digest in artifacts.items()) for scope in tasks[key]['write_scope'])
                    if not complete:
                        valid = False; reason = 'declared-output-missing-or-excluded'
                    elif record.get('status') == 'ACCEPTED':
                        valid = record.get('artifacts') == artifacts
                    else:
                        valid = inventory['fingerprint'] == worker.get('last_source_fingerprint')
                        baseline = record.get('baseline_inventory')
                        if baseline is None:
                            valid = False; reason = 'missing-pre-execution-workspace-baseline'
                        else:
                            current = _visible_inventory(root, tasks[key])
                            outside = sorted(path for path in set(baseline) | set(current)
                                             if baseline.get(path) != current.get(path)
                                             and not _in_scope(path, tasks[key]['write_scope']))
                            record['out_of_scope_changes'] = outside
                            if outside:
                                valid = False; reason = 'writes-outside-declared-scope'
                        if valid:
                            record['artifacts'] = artifacts
                record.update(status='ACCEPTED' if valid else 'BLOCKED', acceptance=acceptance,
                              blocker=None if valid else reason)
            else:
                stage = 'DISPATCH_PENDING' if record['status'] == 'DISPATCH_PENDING' and runtime['status'] == 'READY' else runtime['status']
                record.update(status=stage, blocker=runtime.get('capability_reason') or runtime.get('failure'))
        occupied = []
        for other in state['coordination']['goals'].values():
            if other['plan']['project_id'] != plan['project_id']:
                continue
            specs = {task['id']: task for task in other['plan']['tasks']}
            for key, record in other['tasks'].items():
                runtime_status = workers.get(record.get('worker_task_id'), {}).get('status')
                if record.get('worker_task_id') and record['status'] in HELD and runtime_status not in c.TERMINAL:
                    occupied.append((specs[key]['write_scope'], record.get('worker_task_id')))
        active = sum(bool(record.get('worker_task_id')) and record['status'] in RUNNING for record in goal['tasks'].values())
        # Shared-workspace tasks execute serially; existing native subagents may parallelize within a task.
        limit = 1 if root else plan.get('max_parallel', 3)
        for key, task in tasks.items():
            record = goal['tasks'][key]
            if record.get('worker_task_id'):
                continue
            dependencies = [goal['tasks'][dependency]['status'] for dependency in _ancestors(plan, task)]
            if any(status != 'ACCEPTED' for status in dependencies):
                record.update(status='BLOCKED_DEPENDENCY', blocker='dependency-not-accepted')
                continue
            if active >= limit or any(_overlap(task['write_scope'], scopes) for scopes, _ in occupied):
                record.update(status='READY', blocker='capacity-or-write-scope-busy')
                continue
            worker_plan, context = _compile(goal, task)
            worker_id = 'company-' + e.digest([identifier, key, goal['plan_hash']])[:40]
            record.update(status='DISPATCH_PENDING', blocker=None, worker_task_id=worker_id,
                          worker_plan_hash=e.digest(worker_plan), worker_plan=worker_plan, context=context)
            if root:
                record['baseline_inventory'] = _visible_inventory(root, task)
            occupied.append((task['write_scope'], worker_id)); active += 1
        records = list(goal['tasks'].values())
        goal['status'] = ('DONE' if all(x['status'] == 'ACCEPTED' for x in records) else
                          'RUNNING' if any(x['status'] in RUNNING for x in records) else 'BLOCKED')
        pending = [(key, copy.deepcopy(record)) for key, record in goal['tasks'].items()
                   if record['status'] == 'DISPATCH_PENDING']
        return (None if goal == original else {}), pending
    pending = store.update(reserve, 'Reconcile task graph and reserve ready work before dispatch')
    for key, record in pending:
        w.register_worker(store, record['worker_task_id'], record['worker_plan'])
        def acknowledge(state):
            goal = _goal(state, identifier); current = goal['tasks'][key]
            require(current['worker_plan_hash'] == record['worker_plan_hash'], 'Dispatch reservation changed')
            context_path = 'coordination/' + identifier + '/handoffs/' + key + '.json'
            if current['status'] == 'DISPATCHED' and current.get('context_path') == context_path:
                return None, None
            if current['status'] == 'DISPATCH_PENDING':
                current['status'] = 'DISPATCHED'
            current['context_path'] = context_path
            return {context_path: c.encoded(current['context'])}, None
        store.update(acknowledge, 'Acknowledge worker registration and persist bound handoff')
    return status(store, identifier)


def status(store, identifier):
    _, state = store.read()
    goal = copy.deepcopy(_goal(state, identifier))
    goal.pop('plan')
    for task in goal['tasks'].values():
        task.pop('worker_plan', None); task.pop('context', None); task.pop('baseline_inventory', None)
    return {'goal_id': identifier, **goal, 'model_execution_proven_by_coordinator': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    commands = parser.add_subparsers(dest='action', required=True)
    create = commands.add_parser('register'); create.add_argument('--plan', required=True)
    review = commands.add_parser('approve')
    for field in ('goal-id', 'reviewer', 'plan-hash', 'evidence'):
        review.add_argument('--' + field, required=True)
    advance = commands.add_parser('reconcile'); advance.add_argument('--goal-id', required=True); advance.add_argument('--project-root')
    inspect = commands.add_parser('status'); inspect.add_argument('--goal-id', required=True)
    args = parser.parse_args(argv); store = c.Store(args.repository)
    if args.action == 'register':
        result = register(store, json.loads(Path(args.plan).read_text()))
    elif args.action == 'approve':
        result = approve(store, args.goal_id, args.reviewer, args.plan_hash, args.evidence)
    elif args.action == 'reconcile':
        result = reconcile(store, args.goal_id, args.project_root)
    else:
        result = status(store, args.goal_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
