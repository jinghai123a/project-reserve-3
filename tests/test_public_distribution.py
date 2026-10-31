"""Exercise bundled company roles through the existing activation entry point."""
import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('public_activate_config', ROOT / 'deploy/activate_config.py')
ACTIVATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACTIVATION)


class PublicDistributionTest(unittest.TestCase):
    def test_smoke_context_contains_every_local_docker_copy_input(self):
        script = (ROOT / 'deploy/smoke.sh').read_text()
        # Execute the real context-building function without Docker or cleanup.
        body = script.split('prepare_context() {\n', 1)[1].split('\n}\n', 1)[0]
        function = 'prepare_context() {\n' + body + '\n}\nprepare_context "$1"\n'
        with tempfile.TemporaryDirectory() as directory:
            context = Path(directory) / 'context'
            subprocess.run(['bash', '-euo', 'pipefail', '-c', function, 'smoke-context', str(context)],
                           cwd=ROOT, check=True, capture_output=True, text=True)
            for line in (context / 'Dockerfile').read_text().splitlines():
                if not line.startswith('COPY ') or line.startswith('COPY --from='):
                    continue
                for source in shlex.split(line)[1:-1]:
                    self.assertTrue((context / source).exists(), 'Missing smoke COPY input: ' + source)
            self.assertEqual((context / '.codex/config.toml').read_bytes(), (ROOT / '.codex/config.toml').read_bytes())
            roles = sorted((context / '.codex/agents').glob('*.toml'))
            self.assertEqual(len(roles), 28)
            for role in roles:
                self.assertEqual(role.read_bytes(), (ROOT / '.codex/agents' / role.name).read_bytes())
            self.assertFalse((context / '.codex/agents/smoke_reviewer.toml').exists())
            self.assertEqual((context / '.codex/auth.json').read_text(), 'SMOKE_EXCLUDED_FILE\n')
            self.assertEqual((context / 'deploy/company-rules/.codex/auth.json').read_text(), 'SMOKE_EXCLUDED_FILE\n')
            self.assertTrue((context / 'deploy/smoke-excluded/.codex/marker').is_file())

    def test_all_bundled_roles_activate_and_preserve_authentication(self):
        source = ROOT / '.codex'
        organization = json.loads((ROOT / 'company/organization.json').read_text())
        config = tomllib.loads((source / 'config.toml').read_text())
        roles = {name: item for name, item in config['agents'].items() if isinstance(item, dict)}
        self.assertEqual(set(roles), set(organization['roles']))
        self.assertEqual(len(roles), 28)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / 'auth.json').write_text('existing-auth-fixture')
            result = ACTIVATION.activate(source, target)
            self.assertTrue(result['activated'])
            self.assertEqual(result['files'], len(roles) + 1)
            self.assertTrue(ACTIVATION.activate(source, target)['activated'])
            for role in roles.values():
                relative = role['config_file']
                self.assertEqual((target / relative).read_bytes(), (source / relative).read_bytes())
                instructions = tomllib.loads((target / relative).read_text())['developer_instructions']
                self.assertIn('方案驱动与连续推进', instructions)
                self.assertIn('.company/stages/', instructions)
                for unavailable in ('company_memory.py', 'retrospective.py'):
                    self.assertNotIn(unavailable, instructions)
            self.assertEqual((target / 'auth.json').read_text(), 'existing-auth-fixture')

    def test_bundled_role_conflict_keeps_all_existing_files(self):
        source = ROOT / '.codex'
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            ACTIVATION.activate(source, target)
            role = target / 'agents/delivery_commander.toml'
            role.write_text('developer_instructions = "Operator-owned role."\n')
            (target / 'auth.json').write_text('existing-auth-fixture')
            before = {str(path.relative_to(target)): path.read_bytes()
                      for path in target.rglob('*') if path.is_file()}
            with self.assertRaisesRegex(ValueError, 'Existing Codex configuration preserved'):
                ACTIVATION.activate(source, target)
            after = {str(path.relative_to(target)): path.read_bytes()
                     for path in target.rglob('*') if path.is_file()}
            self.assertEqual(before, after)

    def test_declared_image_role_inputs_match_environment_and_activation(self):
        prefix = '/opt/cloudkit/deploy/company-rules/'
        copies = []
        for line in (ROOT / 'Dockerfile').read_text().splitlines():
            if not line.startswith('COPY '):
                continue
            parts = shlex.split(line)
            if len(parts) == 3 and parts[0] == 'COPY' and parts[2].startswith(prefix):
                copies.append((ROOT / parts[1], parts[2][len(prefix):]))
        self.assertTrue(copies, 'Docker image must contain explicit public company configuration')
        with tempfile.TemporaryDirectory() as directory:
            image_rules = Path(directory)
            for source, destination in copies:
                target = image_rules / destination
                self.assertTrue(source.exists(), str(source))
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
            self.assertTrue((image_rules / 'company/organization.json').is_file())
            self.assertEqual(len(list((image_rules / '.codex/agents').glob('*.toml'))), 28)
            self.assertFalse(list(image_rules.rglob('auth.json')))
            activated = image_rules / 'test-home'
            self.assertTrue(ACTIVATION.activate(image_rules / '.codex', activated)['activated'])
        environment = dict(line.split('=', 1) for line in (ROOT / 'deploy/.env.example').read_text().splitlines()
                           if line and not line.startswith('#') and '=' in line)
        self.assertEqual(environment['CLOUDKIT_AGENT_CONFIG_SOURCE'], prefix + '.codex')


if __name__ == '__main__':
    unittest.main()
