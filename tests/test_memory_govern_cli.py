"""Exercise real CLI policy selection without installing an embedding backend."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from cloudkit.runtime import memoryctl


class MemoryGovernCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='memory-govern-cli-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root/'state'
        self.policy = json.loads(Path(memoryctl.__file__).with_name('storage-policy.json').read_text())
        self.policy['limits'].update(partition_bytes=512*1024*1024, max_active_facts=347, single_note_bytes=4096)
        self.policy_path = self.root/'custom-policy.json'
        self.policy_path.write_text(json.dumps(self.policy))

    def cli(self, *arguments, success=True):
        environment = dict(os.environ, CLOUDKIT_RUNTIME_ROOT=str(self.root/'uninstalled-runtime'))
        result = subprocess.run([sys.executable, str(Path(memoryctl.__file__)),
            '--project', 'fixture/project', '--store-root', str(self.state), 'govern', *arguments],
            env=environment, capture_output=True, text=True, timeout=10)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def test_govern_without_policy_preserves_custom_limits_and_file(self):
        configured = self.cli('--policy', str(self.policy_path))
        saved = Path(configured['storage']['policy_path'])
        before = saved.read_bytes(), saved.stat().st_mtime_ns
        observed = self.cli()
        self.assertTrue(observed['enabled'])
        self.assertNotIn('configured', observed)
        self.assertEqual(observed['limits'], self.policy['limits'])
        self.assertEqual((saved.read_bytes(), saved.stat().st_mtime_ns), before)

    def test_govern_without_existing_policy_reports_disabled(self):
        observed = self.cli()
        self.assertFalse(observed['enabled'])
        self.assertNotIn('configured', observed)
        self.assertEqual(list(self.state.rglob('storage-policy.json')), [])

    def test_explicit_policy_configures_and_explicit_replacement_still_works(self):
        result = self.cli('--policy', str(self.policy_path))
        self.assertTrue(result['configured'])
        self.assertTrue(result['storage']['enabled'])
        self.assertEqual(result['storage']['limits'], self.policy['limits'])
        self.policy['limits']['max_active_facts'] = 123
        self.policy_path.write_text(json.dumps(self.policy))
        updated = self.cli('--policy', str(self.policy_path))
        self.assertEqual(updated['storage']['limits']['max_active_facts'], 123)
        self.assertEqual(self.cli()['limits']['max_active_facts'], 123)

    def test_runtime_migration_requires_explicit_policy_and_preserves_existing(self):
        result = self.cli('--policy', str(self.policy_path))
        saved = Path(result['storage']['policy_path'])
        before = saved.read_bytes()
        failed = self.cli('--migrate-runtime', success=False)
        self.assertIn('Runtime migration requires an explicit policy file', failed.stderr)
        self.assertEqual(saved.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
