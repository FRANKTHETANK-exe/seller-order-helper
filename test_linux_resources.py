"""Linux metadata fixtures on any host; these do not certify Linux execution."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import linux_resources as linux
import runtime_guard as guard


class LinuxResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        self.cg = self.root / 'cgroup'
        (self.proc / 'self').mkdir(parents=True)
        (self.cg / 'user.slice/session').mkdir(parents=True)
        (self.proc / 'self/cgroup').write_text('0::/user.slice/session\n')
        (self.proc / 'self/mountinfo').write_text('30 20 0:30 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n')
        (self.proc / 'meminfo').write_text('MemTotal: 16777216 kB\nMemAvailable: 12582912 kB\nSwapTotal: 1048576 kB\nSwapFree: 1048576 kB\n')
        (self.cg / 'cgroup.controllers').write_text('cpuset cpu io memory pids\n')
        for path in (self.cg / 'user.slice', self.cg / 'user.slice/session'):
            for name, value in [('memory.max', 'max'), ('memory.high', 'max'), ('cpu.max', 'max 100000')]:
                (path / name).write_text(value + '\n')

    def measure(self, *, cpus=4):
        with patch('linux_resources.os.getloadavg', return_value=(1.2, 1.0, 1.0), create=True), \
             patch('linux_resources.os.sched_getaffinity', return_value=set(range(cpus)), create=True), \
             patch('linux_resources.shutil.disk_usage', return_value=SimpleNamespace(free=80 * linux.GIB)):
            return linux.snapshot(self.root, proc_root=self.proc, cgroup_root=self.cg)

    def test_visible_unrestricted_host_keeps_guard_thresholds(self):
        value = self.measure()
        self.assertEqual(value['memory_pressure_free_percent'], 75)
        self.assertEqual(value['swap_used_mib'], 0)
        self.assertEqual(value['cgroup_ancestors_checked'], 3)
        self.assertEqual(value['cpu_affinity_count'], 4)
        guard.guard(value, {'models': []}, check_controller=False)
        self.assertIn('not macOS', value['memory_measurement'])

    def test_duplicate_missing_or_inconsistent_memory_is_held(self):
        valid = (self.proc / 'meminfo').read_text()
        for text in (valid + 'MemTotal: 1 kB\n', valid.replace('MemAvailable', 'Unknown'),
                     valid.replace('SwapFree: 1048576', 'SwapFree: 1048577'),
                     valid.replace('MemAvailable: 12582912', 'MemAvailable: 99999999'),
                     valid.replace('MemTotal: 16777216', 'MemTotal: 0')):
            with self.subTest(text=text):
                with self.assertRaises(linux.LinuxMeasurementError): linux.parse_meminfo(text)

    def test_low_absolute_memory_or_cpu_count_is_held(self):
        with self.assertRaises(linux.LinuxMeasurementError): self.measure(cpus=2)
        p = self.proc / 'meminfo'
        p.write_text(p.read_text().replace('MemAvailable: 12582912', 'MemAvailable: 1048576'))
        with self.assertRaises(guard.PilotStop):
            guard.guard(self.measure(), {'models': []}, check_controller=False)

    def test_startup_floor_is_not_reapplied_to_own_model_memory(self):
        from local_model import MODEL
        p = self.proc / 'meminfo'
        p.write_text('MemTotal: 7596300 kB\nMemAvailable: 3891072 kB\n'
                     'SwapTotal: 4194300 kB\nSwapFree: 4194300 kB\n')
        value = self.measure()
        self.assertLess(value['memory_available_bytes'], 4 * linux.GIB)
        self.assertGreater(value['memory_pressure_free_percent'], 40)
        with self.assertRaises(guard.PilotStop):
            guard.guard(value, {'models': []}, check_controller=False)
        # During loading / inference only the already-admitted pinned request
        # can use its memory. Startup and post-cleanup still require 4 GiB.
        guard.guard(value, {'models': []}, own_model=MODEL, check_controller=False)
        guard.guard(value, {'models': [{'name':MODEL,'size_vram':0}]},
                    own_model=MODEL, check_controller=False)
        with self.assertRaises(guard.PilotStop):
            guard.guard(value, {'models': []}, own_model='unrecognized', check_controller=False)

    def test_running_model_still_holds_on_low_percentage_swap_or_another_model(self):
        from local_model import MODEL
        value = self.measure()
        for changed, loaded in [
            ({'memory_pressure_free_percent':39.99}, {'models': []}),
            ({'swap_used_mib':0.001}, {'models': []}),
            ({'disk_free_bytes':19*linux.GIB}, {'models': []}),
            ({}, {'models': [{'name':'other','size_vram':0}]}),
        ]:
            with self.subTest(changed=changed, loaded=loaded):
                with self.assertRaises(guard.PilotStop):
                    guard.guard({**value, **changed}, loaded, own_model=MODEL,
                                check_controller=False)

    def test_zero_swap_rule_is_preserved(self):
        p = self.proc / 'meminfo'
        p.write_text(p.read_text().replace('SwapFree: 1048576', 'SwapFree: 1048575'))
        with self.assertRaises(guard.PilotStop) as error:
            guard.guard(self.measure(), {'models': []}, check_controller=False)
        self.assertEqual(error.exception.failure_code, 'SWAP_NONZERO')

    def test_memory_percentage_rule_is_preserved(self):
        p = self.proc / 'meminfo'
        p.write_text(p.read_text().replace('MemAvailable: 12582912', 'MemAvailable: 5242880'))
        with self.assertRaises(guard.PilotStop):
            guard.guard(self.measure(), {'models': []}, check_controller=False)

    def test_limits_on_any_ancestor_are_held(self):
        for parent in (self.cg, self.cg / 'user.slice', self.cg / 'user.slice/session'):
            for name, value in [('memory.max', '4294967296'), ('memory.high', '4294967296'), ('cpu.max', '100000 100000')]:
                with self.subTest(parent=parent, name=name):
                    p = parent / name
                    before = p.read_bytes() if p.exists() else None
                    p.write_text(value)
                    with self.assertRaises(linux.LinuxMeasurementError): self.measure()
                    if before is None: p.unlink()
                    else: p.write_bytes(before)

    def test_v1_hidden_or_traversing_cgroup_is_held(self):
        p = self.proc / 'self/cgroup'
        for value in ('1:memory:/\n', '0::/../../host\n', '0::/./host\n', '0:://host\n', '0::/\n1:cpu:/\n'):
            p.write_text(value)
            with self.assertRaises(linux.LinuxMeasurementError): self.measure()

    def test_hidden_mount_or_missing_controllers_is_held(self):
        p = self.proc / 'self/mountinfo'
        original = p.read_text()
        p.write_text(original.replace(' / /sys', ' /hidden /sys'))
        with self.assertRaises(linux.LinuxMeasurementError): self.measure()
        p.write_text(original)
        (self.cg / 'cgroup.controllers').write_text('cpu\n')
        with self.assertRaises(linux.LinuxMeasurementError): self.measure()

    def test_missing_or_oversized_metadata_is_held(self):
        p = self.proc / 'meminfo'
        p.write_bytes(b'x' * 65537)
        with self.assertRaises(linux.LinuxMeasurementError): self.measure()
        p.unlink()
        with self.assertRaises(linux.LinuxMeasurementError): self.measure()

    def test_linux_dispatch_never_executes_mac_commands(self):
        value = self.measure()
        with patch('runtime_guard.sys.platform', 'linux'), \
             patch('linux_resources.snapshot', return_value=value), \
             patch('runtime_guard.command', side_effect=AssertionError('Mac command on Linux')):
            self.assertEqual(guard.local_resources(), value)
        with patch('runtime_guard.sys.platform', 'linux'), \
             patch('linux_resources.snapshot', side_effect=linux.LinuxMeasurementError('fixture path')):
            with self.assertRaises(guard.PilotStop) as error: guard.local_resources()
            self.assertEqual(error.exception.failure_code, 'RESOURCE_ADMISSION_DENIED')
            self.assertNotIn('fixture path', str(error.exception))

    def test_linux_standalone_keeps_digest_loaded_model_and_queue_contract(self):
        from local_model import MODEL, MODEL_DIGEST
        replies = {'/api/version': {'version': 'fixture'}, '/api/tags': {'models': [{'name': MODEL, 'digest': MODEL_DIGEST}]}, '/api/ps': {'models': []}}
        value = self.measure()
        with patch('runtime_guard.sys.platform', 'linux'), \
             patch('runtime_guard.get_json', side_effect=lambda route: replies[route]), \
             patch('runtime_guard.local_resources', return_value=value), \
             patch('runtime_guard.queue_counts', side_effect=AssertionError('controller queue access')):
            result = guard.inspect_standalone({MODEL: MODEL_DIGEST})
            self.assertEqual(result['admission'], 'portable_local_checks_passed')
            replies['/api/ps'] = {'models': [{'name': 'other', 'size_vram': 1}]}
            with self.assertRaises(guard.PilotStop): guard.inspect_standalone({MODEL: MODEL_DIGEST})
            replies['/api/ps'] = {'models': []}
            replies['/api/tags']['models'][0]['digest'] = '0' * 64
            with self.assertRaises(guard.PilotStop): guard.inspect_standalone({MODEL: MODEL_DIGEST})

    def test_windows_remains_held_before_local_provider_calls(self):
        from local_model import GraniteClient, LocalModelHeld, MODEL, MODEL_DIGEST
        with patch('runtime_guard.sys.platform', 'win32'), patch('runtime_guard.get_json') as health:
            with self.assertRaises(LocalModelHeld): GraniteClient(standalone_demo=True)
            with self.assertRaises(guard.PilotStop): guard.inspect_standalone({MODEL: MODEL_DIGEST})
            health.assert_not_called()


if __name__ == '__main__': unittest.main()
