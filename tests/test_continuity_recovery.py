"""Command continuation on persistent and replaced runners, using real Git state."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cloudkit.runtime import continuity as c


def git(root, *args):
    return subprocess.check_output(
        ['git', '-C', str(root), *args], text=True,
        stderr=subprocess.DEVNULL).strip()


class CommandRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='command-recovery-', dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.remote = self.base / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True)
        self.control = self.base / 'control'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(self.control)],
                       check=True, capture_output=True)
        git(self.control, 'switch', '-c', 'main')
        (self.control / 'README.md').write_text('Command recovery fixture\n')
        self.commit('project source')
        source_sha = git(self.control, 'rev-parse', 'HEAD')
        git(self.control, 'push', 'origin', 'main')
        git(self.control, 'switch', '-c', 'automation/state')
        (self.control / 'STATE.json').write_text(json.dumps({
            'schema_version': 1, 'status': 'IDLE',
            'execution_route': 'chatgpt-private-runner'}))
        self.commit('control state')
        git(self.control, 'push', 'origin', 'automation/state')
        self.store = c.Store(self.control)
        self.runs = self.base / 'runs'
        environment = patch.dict(os.environ, {
            'CLOUDKIT_RUNS_ROOT': str(self.runs),
            'GITHUB_REPOSITORY': 'fixture/project'})
        environment.start()
        self.addCleanup(environment.stop)
        self.plan = {
            'project_id': 'fixture/project', 'branch': 'main',
            'goal_id': 'recover-command', 'source_sha': source_sha,
            'acceptance_version': '1', 'steps': [
                {'id': 'build', 'outputs': ['count.txt'], 'routes': [{
                    'id': 'python', 'independence_key': 'python-build',
                    'idempotent': True,
                    'command': [sys.executable, '-c',
                        "from pathlib import Path; p=Path('count.txt'); "
                        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"]}]},
                {'id': 'accept', 'kind': 'acceptance', 'depends_on': ['build'],
                 'inputs': ['count.txt'], 'outputs': ['accepted.txt'], 'routes': [{
                    'id': 'python', 'independence_key': 'python-accept',
                    'idempotent': True,
                    'command': [sys.executable, '-c',
                        "from pathlib import Path; assert Path('count.txt').read_text()=='1'; "
                        "Path('accepted.txt').write_text('passed')"]}]}
            ]}
        c.register(self.store, 'task', 300, self.plan, kind='command')

    def commit(self, message):
        git(self.control, 'add', '.')
        git(self.control, '-c', 'user.name=Fixture', '-c',
            'user.email=fixture@example.invalid', 'commit', '-qm', message)

    def tick(self, event, root=None):
        return c.run_round(self.store, 'task', event, 'owner-' + event, root)

    def test_same_machine_next_round_restores_without_replaying_build(self):
        first = self.tick('first')
        self.assertEqual(first['status'], 'READY', first)
        first_root = Path(first['workspace_path'])
        # An old process or a user may still be touching the previous checkout.
        (first_root / 'count.txt').write_text('unrelated local change')
        second = self.tick('second')
        self.assertEqual(second['status'], 'READY', second)
        second_root = Path(second['workspace_path'])
        self.assertNotEqual(first_root, second_root)
        self.assertEqual((first_root / 'count.txt').read_text(), 'unrelated local change')
        self.assertEqual((second_root / 'count.txt').read_text(), '1')
        self.assertEqual((second_root / 'accepted.txt').read_text(), 'passed')
        final = self.tick('final')
        self.assertEqual(final['status'], 'DONE', final)
        self.assertTrue(final['passed'])
        self.assertEqual(self.tick('after-done')['reason'], 'DONE')

    def test_new_process_and_empty_runner_restore_durable_checkpoint(self):
        first = self.tick('before-restart')
        self.assertEqual(first['status'], 'READY', first)
        shutil.rmtree(self.runs)
        code = (
            'import json,sys; from cloudkit.runtime import continuity as c; '
            'print(json.dumps(c.run_round(c.Store(sys.argv[1]), '
            '"task", "after-restart", "replacement-owner")))')
        result = subprocess.run([sys.executable, '-c', code, str(self.control)],
                                check=True, capture_output=True, text=True)
        resumed = json.loads(result.stdout)
        self.assertEqual(resumed['status'], 'READY', resumed)
        root = Path(resumed['workspace_path'])
        self.assertEqual((root / 'count.txt').read_text(), '1')
        self.assertEqual((root / 'accepted.txt').read_text(), 'passed')
        self.assertEqual(self.tick('complete')['status'], 'DONE')

    def test_existing_default_checkout_with_wrong_identity_is_untouched(self):
        existing = self.runs / 'task' / 'project'
        existing.mkdir(parents=True)
        git(existing, 'init', '-q', '-b', 'personal')
        git(existing, 'remote', 'add', 'origin', 'https://github.com/other/work.git')
        (existing / 'personal.txt').write_text('uncommitted user work')
        result = self.tick('new-attempt')
        self.assertEqual(result['status'], 'READY', result)
        self.assertEqual((existing / 'personal.txt').read_text(), 'uncommitted user work')
        self.assertEqual(git(existing, 'remote', 'get-url', 'origin'),
                         'https://github.com/other/work.git')
        self.assertFalse((existing / 'count.txt').exists())

    def test_explicit_wrong_identity_rejected_before_snapshot_overwrite(self):
        self.assertEqual(self.tick('first')['status'], 'READY')
        wrong = self.base / 'wrong' / 'project'
        subprocess.run(['git', 'clone', '-q', str(self.control), str(wrong)],
                       check=True, capture_output=True)
        git(wrong, 'switch', '-C', 'main', self.plan['source_sha'])
        git(wrong, 'remote', 'set-url', 'origin', 'https://github.com/other/project.git')
        (wrong / 'count.txt').write_text('personal work')
        result = self.tick('wrong-identity', wrong)
        self.assertEqual(result['status'], 'BLOCKED', result)
        self.assertIn('origin does not match', result['failure'])
        self.assertEqual((wrong / 'count.txt').read_text(), 'personal work')
        self.assertFalse((wrong.parent / 'runtime' / 'STATE.json').exists())


if __name__ == '__main__':
    unittest.main()
