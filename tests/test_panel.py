"""Policy and /proc regression checks; independent of KVM."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('panel', Path(__file__).resolve().parents[1] / 'panel/vmctl.py')
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)

class PanelTests(unittest.TestCase):
    def test_stat_with_spaces_and_parentheses(self):
        fields = ['S'] + ['0'] * 10 + ['123', '45'] + ['0'] * 10
        with patch.object(Path, 'read_text', return_value='42 (a tricky ) name) ' + ' '.join(fields)):
            self.assertEqual(panel.read_cpu_ticks(42), 168)
            self.assertEqual(panel.read_cpu_ticks(42, 43), 168)

    def test_resource_limits(self):
        for mem, cpus in [(1, 1), (3073, 1), (64, 0), (64, 65)]:
            with self.assertRaises(panel.VmctlError):
                panel.validate_resources(mem, cpus, True)
        with patch.object(panel, 'HOST_CPUS', 2):
            with self.assertRaises(panel.VmctlError):
                panel.validate_resources(64, 4)
            panel.validate_resources(64, 4, True)

    def test_admission_counts_only_other_running_vms(self):
        state = {'a': {'mem_mb': 600, 'alive': True},
                 'b': {'mem_mb': 1000, 'alive': False},
                 'c': {'mem_mb': 100, 'alive': True}}
        with patch.object(panel, 'host_mem_mb', return_value=1000), \
             patch.object(panel, 'is_running', side_effect=lambda vm: vm['alive']):
            panel.admit(state, 'c', 200)  # exactly 80%
            with self.assertRaises(panel.VmctlError):
                panel.admit(state, 'c', 201)

    def test_cli_validation_and_cold_resize(self):
        script = str(Path(panel.__file__))
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, VMCTL_HOME=tmp)
            def run(*args):
                return subprocess.run(['python3', script, *args], env=env, capture_output=True, text=True)
            guest = str(Path(__file__).resolve())
            self.assertEqual(run('create', 'a', '--guest', guest).returncode, 0)
            for args in [('create', 'a', '--guest', guest), ('create', '../bad', '--guest', guest),
                         ('create', 'b', '--guest', '/missing.bin'), ('stats',),
                         ('stats', 'a', '--pid', str(os.getpid())),
                         ('stats', '--pid', str(os.getpid()), '-i', 'nan')]:
                self.assertNotEqual(run(*args).returncode, 0, args)
            self.assertEqual(run('resize', 'a', '--mem', '256').returncode, 0)
            import json
            vm = json.loads((Path(tmp)/'vms.json').read_text())['a']
            self.assertEqual((vm['mem_mb'], vm['vcpus'], vm['pid']), (256, 1, None))
            self.assertEqual(run('delete', 'a').returncode, 0)

    def test_external_pid_monitoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(['python3', panel.__file__, 'stats', '--pid', str(os.getpid()),
                                     '-i', '0.02', '-n', '2'], capture_output=True, text=True,
                                    env=dict(os.environ, VMCTL_HOME=tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('RSS(MiB)', result.stdout)
            self.assertEqual(len(result.stdout.splitlines()), 4)

if __name__ == '__main__':
    unittest.main()
