#!/usr/bin/env python3
"""Rehearse a reviewed three-stage task graph with real Git/subprocess fixtures.

Run from a source checkout with PYTHONPATH=src. No model or cloud service is used.
All repositories and worker files are temporary; only --output is retained.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from cloudkit.runtime import company as co
from cloudkit.runtime import continuity as c
from cloudkit.runtime import worker_supervisor as w


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


def rehearse():
    with tempfile.TemporaryDirectory(prefix='company-rehearsal-') as directory:
        base = Path(directory); remote = base/'control.git'; control = base/'control'; project = base/'project'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        git(remote, 'config', 'receive.autogc', 'false')
        subprocess.run(['git', 'clone', '-q', str(remote), str(control)], check=True, capture_output=True)
        project.mkdir(); git(project, 'init', '-q', '-b', 'main')
        git(project, 'remote', 'add', 'origin', 'https://github.com/fixture/company-rehearsal.git')
        (project/'fixture.py').write_text('''import json,sys
from pathlib import Path
r=json.load(sys.stdin)
if r['kind']=='capability':
 print(json.dumps({'challenge':r['challenge'],'backend_id':r['backend_id'],'capability':'model-worker','model_response':'Explicit subprocess fixture, not a model'}))
else:
 if len(sys.argv)>2: assert Path(sys.argv[2]).read_text()=='complete'
 Path(sys.argv[1]).write_text('complete')
 print(json.dumps({'request_id':r['request_id'],'fixture':True}))
''')
        git(project, 'add', '.')
        git(project, '-c', 'user.name=Rehearsal', '-c', 'user.email=rehearsal@example.invalid', 'commit', '-qm', 'fixture source')
        sha = git(project, 'rev-parse', 'HEAD'); store = c.Store(control)
        w.init_control(store, 'fixture/company-rehearsal', sha)
        tasks = []
        for index, name in enumerate(('specification', 'implementation', 'verification')):
            dependency = tasks[index-1]['id'] if index else None
            argv = [sys.executable, 'fixture.py', name + '.txt'] + ([dependency + '.txt'] if dependency else [])
            worker = {'project_id': 'fixture/company-rehearsal', 'branch': 'main', 'source_sha': sha,
                'goal': 'Produce the ' + name + ' fixture artifact after checking its input.',
                'progress_files': [name + '.txt'], 'acceptance_timeout_seconds': 5,
                'backends': [{'id': 'subprocess-fixture', 'kind': 'command_fixture', 'independence_key': 'fixture-route',
                    'idempotent': True, 'command': argv, 'probe_command': [sys.executable, 'fixture.py'],
                    'timeout_seconds': 5, 'probe_timeout_seconds': 5}],
                'acceptance': [[sys.executable, '-c', "from pathlib import Path;assert Path(" + repr(name + '.txt') + ").read_text()=='complete'"]]}
            tasks.append({'id': name, 'role': name, 'owner': name + '-owner', 'dependencies': [dependency] if dependency else [],
                          'write_scope': [name + '.txt'], 'worker_plan': worker})
        plan = {'schema_version': 1, 'goal_id': 'company-rehearsal', 'author': 'planner',
                'project_id': 'fixture/company-rehearsal', 'branch': 'main', 'source_sha': sha,
                'goal': 'Verify real task dependency, handoff and acceptance control flow.', 'tasks': tasks}
        registered = co.register(store, plan); review = base/'review.txt'
        review.write_text('Fixture reviewer checked acyclic dependencies, immutable outputs and independent acceptance commands.')
        self_review_rejected = False
        try:
            co.approve(store, plan['goal_id'], 'planner', registered['plan_hash'], review)
        except ValueError:
            self_review_rejected = True
        co.approve(store, plan['goal_id'], 'independent-reviewer', registered['plan_hash'], review)
        reports = []; stale_input_rejected = False; missing_workspace_rejected = False
        for index, task in enumerate(tasks):
            current = co.reconcile(store, plan['goal_id'], project)
            worker_id = current['tasks'][task['id']]['worker_task_id']
            if index == 0:
                missing_workspace_rejected = w.run_round(store, worker_id, 'no-workspace', 'fixture-owner').get('reason') == 'bound-workspace-required'
            if index == 1:
                source = project/'specification.txt'; source.write_text('stale')
                rejected = w.run_round(store, worker_id, 'stale-input', 'fixture-owner', project)
                stale_input_rejected = rejected.get('reason') == 'dependency-artifacts-changed'
                source.write_text('complete')
                # Reconnect using another checkout of the same real control remote.
                recovered = base/'recovered-control'
                subprocess.run(['git', 'clone', '-q', str(remote), str(recovered)], check=True, capture_output=True)
                store = c.Store(recovered)
                co.reconcile(store, plan['goal_id'], project)
            report = w.run_round(store, worker_id, 'execute-' + task['id'], 'fixture-owner', project)
            reports.append({'task': task['id'], 'worker_task_id': worker_id, 'status': report['status'],
                            'passed': report.get('passed'), 'actual_model_success_verified': report.get('actual_model_success_verified')})
        final = co.reconcile(store, plan['goal_id'], project)
        head = store.read()[0]; co.reconcile(store, plan['goal_id'], project)
        stable = head == store.read()[0]
        passed = final['status'] == 'DONE' and all(report['passed'] for report in reports)
        passed = passed and self_review_rejected and stale_input_rejected and missing_workspace_rejected and stable
        return {'scope': 'Real local Git and command subprocess fixtures; not model or deployed cloud acceptance',
                'passed': passed, 'goal_status': final['status'], 'plan_hash': registered['plan_hash'],
                'self_review_rejected': self_review_rejected, 'missing_workspace_rejected': missing_workspace_rejected,
                'stale_input_rejected': stale_input_rejected, 'recovered_from_second_control_checkout': True,
                'idle_reconcile_preserved_control_commit': stable, 'tasks': reports,
                'model_called': False, 'cloud_deployed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--output', type=Path)
    args = parser.parse_args(); result = rehearse(); encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(encoded)
    print(encoded, end='')
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
