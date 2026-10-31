"""Offline regressions for transport reconciliation and preservation of auth/config."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dispatch = load('dispatch')
activation_module = load('activate_config')
activate = activation_module.activate
runtime = load('runtime')


class Store:
    repository = Path('/not-used')

    def __init__(self, tasks):
        self.state = {'autonomy_tasks': tasks}

    def read(self):
        return 'sha', copy.deepcopy(self.state)

    def update(self, mutate, message):
        updates, result = mutate(self.state)
        return result


class DeploymentTest(unittest.TestCase):
    def test_missing_or_invalid_identity_fails_before_any_git_operation(self):
        valid = {'CLOUDKIT_CONTROL_REPOSITORY': 'owner/project',
                 'CLOUDKIT_PROJECT_REPOSITORY': 'owner/project'}
        for key in valid:
            for value in (None, '', 'missing-owner', 'https://github.com/owner/project'):
                environment = dict(valid)
                if value is None:
                    del environment[key]
                else:
                    environment[key] = value
                for operation in (runtime.prepare, runtime.store):
                    with self.subTest(key=key, value=value, operation=operation.__name__), \
                         patch.dict('os.environ', environment, clear=True), \
                         patch.object(runtime, 'checked') as git:
                        with self.assertRaisesRegex(ValueError, key):
                            operation()
                        git.assert_not_called()

    def test_explicit_separate_control_repository_remains_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); control = root/'control'
            state = {'control_project_id': 'customer/application', 'work_branch': 'main'}
            fake_store = Mock(); fake_store.read.return_value = ('state-sha', state)
            environment = {'CLOUDKIT_CONTROL_REPOSITORY': 'operator/dedicated-control',
                           'CLOUDKIT_PROJECT_REPOSITORY': 'customer/application',
                           'CLOUDKIT_SOURCE_SHA': 'a'*40, 'CLOUDKIT_ACTIVATE_CONTROL_CONFIG': '0'}
            with patch.dict('os.environ', environment, clear=True), \
                 patch.object(runtime, 'CONTROL', control), patch.object(runtime, 'DATA', root), \
                 patch.object(runtime, 'checked', return_value='') as git, \
                 patch.object(runtime, 'store', return_value=fake_store), \
                 patch.object(runtime.worker_supervisor, 'init_control', return_value={'initialized': True}) as initialize:
                self.assertEqual(runtime.prepare(), {'initialized': True})
                self.assertIn((['git', 'clone', 'https://github.com/operator/dedicated-control.git', str(control)],),
                              [call.args for call in git.call_args_list])
                initialize.assert_called_once_with(fake_store, 'customer/application', 'a'*40, 'main')

    def test_blocked_without_wakeup_does_not_stop_ready_task(self):
        store = Store({'blocked': {'status': 'WAITING_CAPABILITY', 'next_wakeup_at': None},
                       'ready': {'status': 'READY', 'next_wakeup_at': 9, 'outbox': None}})
        enqueue = Mock(return_value={'enqueued': True})
        result = dispatch.reconcile(store, enqueue, clock=lambda: 10)
        self.assertEqual(len(result), 1)
        self.assertEqual(enqueue.call_args.args[1], 'ready')

    def test_one_company_failure_does_not_stop_independent_goals(self):
        store = Store({})
        store.state['coordination'] = {'goals': {
            'bad': {'status': 'RUNNING', 'review': {'plan_hash': 'one'}, 'plan': {'tasks': []}},
            'good': {'status': 'RUNNING', 'review': {'plan_hash': 'two'}, 'plan': {'tasks': []}},
            'unreviewed': {'status': 'PLANNED', 'review': None, 'plan': {'tasks': []}},
            'finished': {'status': 'DONE', 'review': {'plan_hash': 'three'}, 'plan': {'tasks': []}},
        }}
        advance = Mock(side_effect=[ValueError('stale input'), {'status': 'RUNNING'}])
        results = dispatch.reconcile_companies(store, advance)
        self.assertEqual([x['goal_id'] for x in results], ['bad', 'good'])
        self.assertEqual([x['status'] for x in results], ['BLOCKED', 'RUNNING'])
        self.assertEqual(advance.call_count, 2)

    def test_unbound_dependency_goal_waits_without_dispatch(self):
        store = Store({})
        store.state['coordination'] = {'goals': {'goal': {'status': 'REVIEWED', 'review': {'plan_hash': 'one'},
            'plan': {'tasks': [{'dependencies': ['source']}]}}}}
        advance = Mock()
        self.assertEqual(dispatch.reconcile_companies(store, advance)[0]['status'], 'WAITING_WORKSPACE')
        advance.assert_not_called()

    def test_due_retry_recreates_outbox_but_future_and_terminal_do_not(self):
        task = lambda status, due: {'status': status, 'next_wakeup_at': due, 'outbox': None}
        store = Store({'due': task('RETRY_WAIT', 9), 'future': task('RETRY_WAIT', 11),
                       'done': task('DONE', 0), 'waiting': task('WAITING_CAPABILITY', 0)})
        enqueue = Mock(return_value={'enqueued': True})
        result = dispatch.reconcile(store, enqueue, clock=lambda: 10)
        self.assertEqual(len(result), 1)
        self.assertEqual(enqueue.call_args.args[1], 'due')
        self.assertTrue(store.state['autonomy_tasks']['due']['outbox']['event_id'])
        self.assertIsNone(store.state['autonomy_tasks']['future']['outbox'])

    def test_control_project_change_rejected(self):
        with patch.dict('os.environ', {'CLOUDKIT_PROJECT_REPOSITORY': 'owner/new', 'CLOUDKIT_PROJECT_BRANCH': 'main'}):
            with self.assertRaises(ValueError):
                runtime.validate_project({'control_project_id': 'owner/old', 'work_branch': 'main'})
            runtime.validate_project({'control_project_id': 'owner/new', 'work_branch': 'main'})

    def test_lease_blocks_delivery(self):
        store = Store({'live': {'status': 'ACTIVE', 'next_wakeup_at': 0,
                                'lease': {'expires_at': 12}, 'outbox': None}})
        enqueue = Mock()
        self.assertEqual(dispatch.reconcile(store, enqueue, clock=lambda: 10), [])
        enqueue.assert_not_called()

    def test_config_preserves_auth_and_refuses_different_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            source.mkdir(); target.mkdir()
            (source / 'config.toml').write_text('[agents]\nmax_threads = 4\n')
            (target / 'auth.json').write_text('private auth fixture')
            self.assertTrue(activate(source, target)['activated'])
            self.assertEqual((target / 'auth.json').read_text(), 'private auth fixture')
            (target / 'config.toml').write_text('model = "existing"\n')
            with self.assertRaises(ValueError):
                activate(source, target)
            self.assertEqual((target / 'config.toml').read_text(), 'model = "existing"\n')

    def test_custom_agent_source_is_independent_from_control_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control, source, target = root/'control', root/'company-rules/.codex', root/'codex-home'
            (control/'.codex').mkdir(parents=True); (source/'agents').mkdir(parents=True); target.mkdir()
            (control/'.codex/config.toml').write_text('[agents]\nmax_threads = 1\n')
            config = '[agents]\nmax_threads = 4\n[agents.reviewer]\nconfig_file = "agents/reviewer.toml"\n'
            (source/'config.toml').write_text(config)
            role = 'developer_instructions = "Review independently."\n'
            (source/'agents/reviewer.toml').write_text(role)
            (target/'auth.json').write_text('private auth fixture')
            environment = {'CODEX_HOME': str(target), 'CLOUDKIT_AGENT_CONFIG_SOURCE': str(source)}
            with patch.object(runtime, 'CONTROL', control), patch.dict('os.environ', environment, clear=True), \
                 patch.dict('sys.modules', {'activate_config': activation_module}):
                self.assertTrue(runtime.activate_agent_config()['activated'])
                self.assertTrue(runtime.activate_agent_config()['activated'])
            self.assertEqual((target/'config.toml').read_text(), config)
            self.assertEqual((target/'agents/reviewer.toml').read_text(), role)
            self.assertEqual((target/'auth.json').read_text(), 'private auth fixture')
            self.assertEqual((control/'.codex/config.toml').read_text(), '[agents]\nmax_threads = 1\n')

    def test_default_agent_source_remains_optional_and_selects_control_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); control, target = root/'control', root/'codex-home'
            target.mkdir(); (target/'auth.json').write_text('private auth fixture')
            with patch.object(runtime, 'CONTROL', control), \
                 patch.dict('os.environ', {'CODEX_HOME': str(target)}, clear=True), \
                 patch.dict('sys.modules', {'activate_config': activation_module}):
                self.assertFalse(runtime.activate_agent_config()['activated'])
                with patch.dict('os.environ', {'CLOUDKIT_AGENT_CONFIG_SOURCE': str(control/'.codex')}):
                    self.assertFalse(runtime.activate_agent_config()['activated'])
                (control/'.codex').mkdir(parents=True)
                config = '[agents]\nmax_threads = 2\n'
                (control/'.codex/config.toml').write_text(config)
                self.assertTrue(runtime.activate_agent_config()['activated'])
            self.assertEqual((target/'config.toml').read_text(), config)
            self.assertEqual((target/'auth.json').read_text(), 'private auth fixture')

    def test_missing_custom_source_or_config_fails_without_touching_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root/'codex-home'; target.mkdir()
            (target/'auth.json').write_text('private auth fixture')
            (target/'config.toml').write_text('model = "existing"\n')
            empty = root/'empty'; empty.mkdir()
            for source in (root/'missing', empty):
                with self.subTest(source=source), patch.object(runtime, 'CONTROL', root/'control'), \
                     patch.dict('os.environ', {'CODEX_HOME': str(target), 'CLOUDKIT_AGENT_CONFIG_SOURCE': str(source)}, clear=True), \
                     patch.dict('sys.modules', {'activate_config': activation_module}):
                    with self.assertRaisesRegex(ValueError, 'CLOUDKIT_AGENT_CONFIG_SOURCE'):
                        runtime.activate_agent_config()
            self.assertEqual((target/'config.toml').read_text(), 'model = "existing"\n')
            self.assertEqual((target/'auth.json').read_text(), 'private auth fixture')

    def test_custom_source_conflict_preserves_all_existing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source, target = root/'source', root/'codex-home'
            source.mkdir(); target.mkdir()
            (source/'config.toml').write_text('[agents]\nmax_threads = 4\n')
            (target/'config.toml').write_text('model = "existing"\n')
            (target/'auth.json').write_text('private auth fixture')
            with patch.object(runtime, 'CONTROL', root/'control'), \
                 patch.dict('os.environ', {'CODEX_HOME': str(target), 'CLOUDKIT_AGENT_CONFIG_SOURCE': str(source)}, clear=True), \
                 patch.dict('sys.modules', {'activate_config': activation_module}):
                with self.assertRaisesRegex(ValueError, 'Existing Codex configuration preserved'):
                    runtime.activate_agent_config()
            self.assertEqual((target/'config.toml').read_text(), 'model = "existing"\n')
            self.assertEqual((target/'auth.json').read_text(), 'private auth fixture')

    def test_prepare_uses_source_activation_and_retains_explicit_opt_out(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); control = root/'control'; (control/'.git').mkdir(parents=True)
            fake_store = Mock()
            fake_store.read.return_value = ('sha', {'control_project_id': 'owner/project', 'work_branch': 'main'})
            environment = {'CLOUDKIT_CONTROL_REPOSITORY': 'owner/project', 'CLOUDKIT_PROJECT_REPOSITORY': 'owner/project',
                           'CLOUDKIT_SOURCE_SHA': 'a'*40, 'CODEX_HOME': str(root/'codex-home')}
            with patch.object(runtime, 'CONTROL', control), patch.object(runtime, 'DATA', root), \
                 patch.dict('os.environ', environment, clear=True), patch.object(runtime, 'checked', return_value=''), \
                 patch.object(runtime, 'store', return_value=fake_store), \
                 patch.object(runtime.worker_supervisor, 'init_control', return_value={'initialized': True}), \
                 patch.object(runtime, 'activate_agent_config') as selected:
                self.assertEqual(runtime.prepare(), {'initialized': True})
                selected.assert_called_once_with()
                selected.reset_mock()
                with patch.dict('os.environ', {'CLOUDKIT_ACTIVATE_CONTROL_CONFIG': '0'}):
                    runtime.prepare()
                selected.assert_not_called()


@unittest.skipUnless(shutil.which('docker'), 'Docker Compose is unavailable locally; CI exercises configuration interpolation')
class ComposeIdentityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        version = subprocess.run(['docker', 'compose', 'version'], capture_output=True, timeout=15)
        if version.returncode:
            raise unittest.SkipTest('Docker Compose plugin is unavailable')

    def render(self, values):
        with tempfile.TemporaryDirectory() as directory:
            environment_file = Path(directory)/'environment.env'
            environment_file.write_text('\n'.join(key + '=' + value for key, value in values.items()) + '\n')
            environment = {key: value for key, value in os.environ.items() if not key.startswith('CLOUDKIT_')}
            return subprocess.run(['docker', 'compose', '--env-file', str(environment_file),
                                   '-f', str(HERE.parent/'docker-compose.yml'), 'config', '--format', 'json'],
                                  capture_output=True, text=True, env=environment, timeout=30)

    def test_template_requires_both_explicit_repository_values(self):
        template = {}
        for line in (HERE/'.env.example').read_text().splitlines():
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1); template[key] = value
        for supplied in (None, 'CLOUDKIT_CONTROL_REPOSITORY', 'CLOUDKIT_PROJECT_REPOSITORY'):
            values = dict(template)
            if supplied:
                values[supplied] = 'customer/project'
            with self.subTest(supplied=supplied):
                result = self.render(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('CLOUDKIT_', result.stderr)

    def test_explicit_company_project_renders_without_infrastructure_defaults(self):
        result = self.render({'CLOUDKIT_CONTROL_REPOSITORY': 'customer/project',
                              'CLOUDKIT_PROJECT_REPOSITORY': 'customer/project'})
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)['services']
        for name in ('worker', 'dispatcher'):
            self.assertEqual(services[name]['environment']['CLOUDKIT_CONTROL_REPOSITORY'], 'customer/project')
            self.assertEqual(services[name]['environment']['CLOUDKIT_PROJECT_REPOSITORY'], 'customer/project')


if __name__ == '__main__':
    unittest.main()
