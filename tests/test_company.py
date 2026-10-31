"""Real local Git coordination contracts; command fixtures are not model proof."""
import copy
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cloudkit.runtime import company as co
from cloudkit.runtime import continuity as c
from cloudkit.runtime import worker_supervisor as w


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True, stderr=subprocess.DEVNULL).strip()


class CompanyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='company-contract-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.remote = self.base/'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True)
        git(self.remote, 'config', 'receive.autogc', 'false')
        self.control = self.base/'control'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(self.control)], check=True, capture_output=True)
        self.store = c.Store(self.control)
        self.project = self.base/'project'; self.project.mkdir()
        git(self.project, 'init', '-q', '-b', 'main')
        git(self.project, 'remote', 'add', 'origin', 'https://github.com/fixture/project.git')
        (self.project/'input.txt').write_text('stable')
        git(self.project, 'add', '.')
        git(self.project, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'source')
        self.sha = git(self.project, 'rev-parse', 'HEAD')
        w.init_control(self.store, 'fixture/project', self.sha)
        self.evidence = self.base/'review.txt'; self.evidence.write_text('Independent review of task inputs, dependencies and acceptance.')
        self.plan = {'schema_version': 1, 'goal_id': 'delivery', 'author': 'planner',
            'project_id': 'fixture/project', 'branch': 'main', 'source_sha': self.sha,
            'goal': 'Complete two independently accepted files', 'tasks': [self.task('one'), self.task('two')]}

    def task(self, identifier, dependencies=None, scope=None):
        target = identifier + '.txt'
        worker = {'project_id': 'fixture/project', 'branch': 'main', 'source_sha': self.sha,
            'goal': 'Write ' + target, 'progress_files': [target], 'backends': [{
                'id': 'fixture', 'independence_key': 'fixture-process', 'kind': 'command_fixture', 'idempotent': True,
                'command': [sys.executable, '-c', "import json,sys;from pathlib import Path;r=json.load(sys.stdin);Path(" + repr(target) + ").write_text('done');print(json.dumps({'request_id':r['request_id']}))"],
                'probe_command': [sys.executable, '-c', "import json,sys;r=json.load(sys.stdin);print(json.dumps({'challenge':r['challenge'],'backend_id':r['backend_id'],'capability':'model-worker','model_response':'command fixture only'}))"],
                'timeout_seconds': 5, 'probe_timeout_seconds': 5}],
            'acceptance': [[sys.executable, '-c', "from pathlib import Path;assert Path(" + repr(target) + ").read_text()=='done'"]],
            'acceptance_timeout_seconds': 5}
        return {'id': identifier, 'role': 'engineer', 'owner': 'engineer-' + identifier,
                'dependencies': dependencies or [], 'write_scope': scope or [target], 'worker_plan': worker}

    def admitted(self):
        result = co.register(self.store, self.plan)
        co.approve(self.store, self.plan['goal_id'], 'reviewer', result['plan_hash'], self.evidence)
        return result

    def mutate_worker(self, worker_id, status, passed=None):
        def mutation(state):
            item = state['autonomy_tasks'][worker_id]; item['status'] = status
            if passed is not None:
                item['worker_supervisor']['acceptance'] = {'passed': passed, 'evidence': [{'fixture': True}]}
            return {}, None
        self.store.update(mutation)

    def test_cycles_missing_dependencies_and_review_identity(self):
        bad = copy.deepcopy(self.plan); bad['tasks'][0]['dependencies'] = ['absent']
        with self.assertRaisesRegex(ValueError, 'Missing dependency'):
            co.register(self.store, bad)
        bad['tasks'][0]['dependencies'] = ['two']; bad['tasks'][1]['dependencies'] = ['one']
        with self.assertRaisesRegex(ValueError, 'cycle'):
            co.register(self.store, bad)
        result = co.register(self.store, self.plan)
        with self.assertRaisesRegex(ValueError, 'author'):
            co.approve(self.store, 'delivery', 'planner', result['plan_hash'], self.evidence)
        with self.assertRaisesRegex(ValueError, 'stale'):
            co.approve(self.store, 'delivery', 'reviewer', 'wrong', self.evidence)
        with self.assertRaisesRegex(ValueError, 'review'):
            co.reconcile(self.store, 'delivery')

    def test_stale_plan_mutation_invalidates_review(self):
        self.admitted()
        def mutation(state):
            state['coordination']['goals']['delivery']['plan']['goal'] = 'changed'
            return {}, None
        self.store.update(mutation)
        with self.assertRaisesRegex(ValueError, 'changed'):
            co.reconcile(self.store, 'delivery')

    def test_dependent_task_cannot_overwrite_accepted_ancestor_scope(self):
        self.plan['tasks'][1]['dependencies'] = ['one']
        self.plan['tasks'][1]['write_scope'] = ['one.txt']
        with self.assertRaisesRegex(ValueError, 'ancestor artifacts'):
            co.register(self.store, self.plan)

    def test_concurrent_reconcilers_register_each_worker_once(self):
        self.admitted()
        alternate = self.base/'alternate-control'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(alternate)], check=True, capture_output=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda store: co.reconcile(store, 'delivery'), [self.store, c.Store(alternate)]))
        self.assertEqual(len(self.store.read()[1]['autonomy_tasks']), 2)
        ids = [{task['worker_task_id'] for task in result['tasks'].values()} for result in results]
        self.assertEqual(ids[0], ids[1])

    def test_overlapping_scope_waits_and_terminal_releases_capacity(self):
        self.plan['tasks'][0]['write_scope'] = ['src']
        self.plan['tasks'][1]['write_scope'] = ['src/main.py']
        self.admitted(); first = co.reconcile(self.store, 'delivery')
        self.assertEqual(first['tasks']['one']['status'], 'DISPATCHED')
        self.assertNotIn('worker_task_id', first['tasks']['two'])
        second = co.reconcile(self.store, 'delivery')
        self.assertNotIn('worker_task_id', second['tasks']['two'])
        self.mutate_worker(first['tasks']['one']['worker_task_id'], 'BLOCKED')
        third = co.reconcile(self.store, 'delivery')
        self.assertEqual(third['tasks']['two']['status'], 'DISPATCHED')

    def test_blocked_dependency_does_not_stop_independent_work(self):
        self.plan['tasks'] = [self.task('one'), self.task('two', ['one']), self.task('three')]
        self.admitted()
        result = co.reconcile(self.store, 'delivery', self.project)
        self.mutate_worker(result['tasks']['one']['worker_task_id'], 'WAITING_CAPABILITY')
        result = co.reconcile(self.store, 'delivery', self.project)
        self.assertEqual(result['tasks']['two']['status'], 'BLOCKED_DEPENDENCY')
        self.assertEqual(result['tasks']['three']['status'], 'DISPATCHED')

    def test_crash_after_reservation_or_partial_registration_is_idempotent(self):
        self.plan['tasks'] = [self.task('one')]
        self.admitted()
        with patch.object(w, 'register_worker', side_effect=RuntimeError('controlled crash')):
            with self.assertRaisesRegex(RuntimeError, 'crash'):
                co.reconcile(self.store, 'delivery')
        reserved = co.status(self.store, 'delivery')['tasks']['one']['worker_task_id']
        _, state = self.store.read(); saved = state['coordination']['goals']['delivery']['tasks']['one']['worker_plan']
        c.register(self.store, reserved, 1800, kind='model', goal=saved['goal'])
        result = co.reconcile(self.store, 'delivery')
        self.assertEqual(result['tasks']['one']['worker_task_id'], reserved)
        self.assertEqual(result['tasks']['one']['status'], 'DISPATCHED')
        self.assertEqual(len(self.store.read()[1]['autonomy_tasks']), 1)

    def test_crash_after_register_before_ack_recovers_same_worker(self):
        self.plan['tasks'] = [self.task('one')]; self.admitted()
        real = w.register_worker
        def register_then_crash(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError('after-register crash')
        with patch.object(w, 'register_worker', side_effect=register_then_crash):
            with self.assertRaises(RuntimeError):
                co.reconcile(self.store, 'delivery')
        result = co.reconcile(self.store, 'delivery')
        self.assertEqual(result['tasks']['one']['status'], 'DISPATCHED')
        self.assertEqual(len(self.store.read()[1]['autonomy_tasks']), 1)

    def test_done_without_independent_acceptance_does_not_complete(self):
        self.plan['tasks'] = [self.task('one')]; self.admitted()
        result = co.reconcile(self.store, 'delivery')
        self.mutate_worker(result['tasks']['one']['worker_task_id'], 'DONE')
        result = co.reconcile(self.store, 'delivery')
        self.assertEqual(result['tasks']['one']['status'], 'BLOCKED')
        self.assertNotEqual(result['status'], 'DONE')

    def test_unchanged_reconcile_does_not_commit_again(self):
        self.admitted(); co.reconcile(self.store, 'delivery')
        co.reconcile(self.store, 'delivery')
        before = self.store.read()[0]
        co.reconcile(self.store, 'delivery')
        self.assertEqual(self.store.read()[0], before)

    def test_real_done_worker_with_missing_declared_output_is_rejected(self):
        self.plan['tasks'] = [self.task('one')]
        worker = self.plan['tasks'][0]['worker_plan']
        worker['backends'][0]['command'] = [sys.executable, '-c', "import json,sys;r=json.load(sys.stdin);print(json.dumps({'request_id':r['request_id']}))"]
        worker['acceptance'] = [[sys.executable, '-c', 'pass']]
        self.admitted(); result = co.reconcile(self.store, 'delivery', self.project)
        report = w.run_round(self.store, result['tasks']['one']['worker_task_id'], 'missing-output', 'fixture', self.project)
        self.assertEqual(report['status'], 'DONE', report)
        result = co.reconcile(self.store, 'delivery', self.project)
        self.assertEqual(result['tasks']['one']['blocker'], 'declared-output-missing-or-excluded')
        self.assertNotEqual(result['status'], 'DONE')

    def test_real_done_worker_cannot_hide_output_using_exclude_paths(self):
        self.plan['tasks'] = [self.task('one')]
        self.plan['tasks'][0]['worker_plan']['exclude_paths'] = ['one.txt']
        self.admitted(); result = co.reconcile(self.store, 'delivery', self.project)
        report = w.run_round(self.store, result['tasks']['one']['worker_task_id'], 'excluded-output', 'fixture', self.project)
        self.assertEqual(report['status'], 'DONE', report)
        result = co.reconcile(self.store, 'delivery', self.project)
        self.assertEqual(result['tasks']['one']['blocker'], 'declared-output-missing-or-excluded')

    def test_real_done_worker_writing_outside_scope_is_rejected(self):
        self.plan['tasks'] = [self.task('one')]
        worker = self.plan['tasks'][0]['worker_plan']
        worker['exclude_paths'] = ['input.txt']
        worker['backends'][0]['command'] = [sys.executable, '-c', "import json,sys;from pathlib import Path;r=json.load(sys.stdin);Path('one.txt').write_text('done');Path('input.txt').write_text('unexpected');print(json.dumps({'request_id':r['request_id']}))"]
        self.admitted(); result = co.reconcile(self.store, 'delivery', self.project)
        report = w.run_round(self.store, result['tasks']['one']['worker_task_id'], 'outside-scope', 'fixture', self.project)
        self.assertEqual(report['status'], 'DONE', report)
        result = co.reconcile(self.store, 'delivery', self.project)
        self.assertEqual(result['tasks']['one']['blocker'], 'writes-outside-declared-scope')
        self.assertEqual(result['tasks']['one']['out_of_scope_changes'], ['input.txt'])
        self.assertEqual((self.project/'input.txt').read_text(), 'unexpected')

    def test_read_only_empty_scope_accepts_actual_verification_without_writes(self):
        self.plan['tasks'] = [self.task('one')]
        task = self.plan['tasks'][0]; task['write_scope'] = []
        worker = task['worker_plan']; worker['progress_files'] = []
        worker['backends'][0]['command'] = [sys.executable, '-c', "import json,sys;r=json.load(sys.stdin);print(json.dumps({'request_id':r['request_id']}))"]
        worker['acceptance'] = [[sys.executable, '-c', "from pathlib import Path;assert Path('input.txt').read_text()=='stable'"]]
        self.admitted(); result = co.reconcile(self.store, 'delivery', self.project)
        report = w.run_round(self.store, result['tasks']['one']['worker_task_id'], 'read-only', 'fixture', self.project)
        self.assertEqual(report['status'], 'DONE', report)
        result = co.reconcile(self.store, 'delivery', self.project)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(result['tasks']['one']['artifacts'], {})

    def test_shared_workspace_real_worker_handoff_and_stale_artifact(self):
        self.plan['tasks'][1]['dependencies'] = ['one']; self.admitted()
        with self.assertRaisesRegex(ValueError, 'project-root'):
            co.reconcile(self.store, 'delivery')
        result = co.reconcile(self.store, 'delivery', self.project)
        worker_id = result['tasks']['one']['worker_task_id']
        report = w.run_round(self.store, worker_id, 'run-one', 'fixture-owner', self.project)
        self.assertEqual(report['status'], 'DONE', report)
        result = co.reconcile(self.store, 'delivery', self.project)
        self.assertEqual(result['tasks']['one']['status'], 'ACCEPTED')
        self.assertEqual(result['tasks']['two']['status'], 'DISPATCHED')
        _, state = self.store.read()
        downstream = state['autonomy_tasks'][result['tasks']['two']['worker_task_id']]['worker_supervisor']['plan']
        self.assertEqual(downstream['required_project_root'], str(self.project.resolve()))
        self.assertIn('one.txt', downstream['goal'])
        self.assertIn('one.txt', downstream['required_input_hashes'])
        (self.project/'one.txt').write_text('modified after acceptance')
        result = co.reconcile(self.store, 'delivery', self.project)
        self.assertEqual(result['tasks']['one']['status'], 'BLOCKED')
        self.assertNotEqual(result['status'], 'DONE')


if __name__ == '__main__':
    unittest.main()
