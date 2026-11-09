"""Offline bounds, policy routing and fake-transport checks; no real inference."""
import contextlib
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from facts import OrderFacts
from suggestions import _prompt
import runtime_guard as guard
from local_model import GraniteClient, LocalModelHeld, MODEL, MODEL_DIGEST, StubClient
try:
    import fcntl
except ImportError:
    fcntl = None


def sample(swap=0.6171875, available=6 * guard.GIB, total=8 * guard.GIB):
    available, total = int(available), int(total)
    return {'resource_platform': 'linux', 'memory_available_bytes': available,
            'memory_total_bytes': total,
            'memory_pressure_free_percent': 100 * available / total,
            'swap_used_mib': swap, 'load_1m': 1.0,
            'disk_free_bytes': 40 * guard.GIB}


class LinuxSmallSwapTests(unittest.TestCase):
    def check(self, value, loaded=None, **kwargs):
        options = {'check_controller': False, 'standalone_demo': True, **kwargs}
        with patch.object(guard.sys, 'platform', 'linux'):
            return guard.guard(value, {'models': []} if loaded is None else loaded, **options)

    def test_total_swap_boundaries_apply_at_startup_and_during_owned_request(self):
        for own in (None, MODEL):
            for swap in (0, 0.6171875, 16):
                with self.subTest(own=own, swap=swap):
                    self.check(sample(swap), own_model=own)
            with self.assertRaises(guard.PilotStop) as caught:
                self.check(sample(16 + 1 / 1024), own_model=own)
            self.assertEqual(caught.exception.failure_code, 'SWAP_NONZERO')

    def test_allowance_requires_exact_linux_standalone_and_no_controller_check(self):
        for platform in ('linux', 'darwin', 'win32'):
            for mode in (True, False, 1, 'yes', None):
                value = sample()
                with self.subTest(platform=platform, mode=mode), \
                     patch.object(guard.sys, 'platform', platform):
                    expected = 16 if platform == 'linux' and mode is True else 0
                    self.assertEqual(guard.swap_policy(standalone_demo=mode)['max_swap_mib'], expected)
                    if expected:
                        guard.guard(value, {'models': []}, check_controller=False, standalone_demo=mode)
                    else:
                        with self.assertRaises(guard.PilotStop) as caught:
                            guard.guard(value, {'models': []}, check_controller=False, standalone_demo=mode)
                        self.assertEqual(caught.exception.failure_code, 'SWAP_NONZERO')
        for controller in (True, 0, None):
            with self.subTest(controller=controller), self.assertRaises(guard.PilotStop) as caught:
                self.check(sample(), check_controller=controller)
            self.assertEqual(caught.exception.failure_code, 'RESOURCE_ADMISSION_DENIED')
        for platform in (None, 'darwin', 'Linux'):
            with self.subTest(metadata=platform), self.assertRaises(guard.PilotStop):
                self.check({**sample(), 'resource_platform': platform})

    def test_malformed_metrics_fail_closed_before_threshold_comparisons(self):
        for key in ('swap_used_mib', 'memory_pressure_free_percent', 'load_1m', 'disk_free_bytes'):
            for value in (None, True, '0', -1, float('nan'), float('inf'), float('-inf'), 2**10000):
                with self.subTest(key=key, value_type=type(value).__name__), \
                     self.assertRaises(guard.PilotStop) as caught:
                    self.check({**sample(), key: value})
                self.assertEqual(caught.exception.failure_code, 'RESOURCE_ADMISSION_DENIED')
        for key in ('memory_available_bytes', 'memory_total_bytes'):
            for value in (None, True, '1', -1, 1.0, float('nan'), 2**10000):
                with self.subTest(key=key, value_type=type(value).__name__), \
                     self.assertRaises(guard.PilotStop):
                    self.check({**sample(), key: value})
        for changed in ({'memory_total_bytes': 0}, {'memory_available_bytes': 9 * guard.GIB},
                        {'memory_pressure_free_percent': 101}, {'memory_pressure_free_percent': 60}):
            with self.subTest(changed=changed), self.assertRaises(guard.PilotStop):
                self.check({**sample(), **changed})
        with self.assertRaises(guard.PilotStop):
            self.check(None)

    def test_accepted_swap_does_not_bypass_other_gates(self):
        cases = [(sample(16, available=4 * guard.GIB - 1), None),
                 (sample(16, available=3 * guard.GIB), MODEL),
                 ({**sample(16), 'load_1m': 6.01}, None),
                 ({**sample(16), 'disk_free_bytes': 20 * guard.GIB - 1}, MODEL)]
        for value, own in cases:
            with self.subTest(own=own, value=value), self.assertRaises(guard.PilotStop):
                self.check(value, own_model=own)
        self.check(sample(16, available=3.5 * guard.GIB), own_model=MODEL)
        with self.assertRaises(guard.PilotStop):
            self.check(sample(), {'models': [{'name': 'foreign', 'size_vram': 0}]}, own_model=MODEL)
        with self.assertRaises(guard.PilotStop):
            self.check(sample(), {'models': [{'name': MODEL, 'size_vram': 11 * guard.GIB}]}, own_model=MODEL)

    def test_preflight_reports_the_same_fixed_policy_and_never_reads_queues(self):
        replies = {'/api/version': {'version': 'fixture'},
                   '/api/tags': {'models': [{'name': MODEL, 'digest': MODEL_DIGEST}]},
                   '/api/ps': {'models': []}}
        with patch.object(guard.sys, 'platform', 'linux'), \
             patch.object(guard, 'get_json', side_effect=lambda route: replies[route]), \
             patch.object(guard, 'local_resources', return_value=sample(16)), \
             patch.object(guard, 'queue_counts', side_effect=AssertionError('queue read')):
            result = guard.inspect_standalone({MODEL: MODEL_DIGEST})
            self.assertEqual(result['swap_policy'], guard.swap_policy(standalone_demo=True))
            self.assertEqual(result['resources']['swap_used_mib'], 16)
            replies['/api/tags']['models'][0]['digest'] = '0' * 64
            with self.assertRaises(guard.PilotStop):
                guard.inspect_standalone({MODEL: MODEL_DIGEST})


@unittest.skipUnless(fcntl is not None and sys.platform != 'win32', 'Native POSIX client test')
class LinuxSwapClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lock = Path(self.temp.name) / 'model.lock'
        self.prompt = _prompt(OrderFacts('SIM-SMALL-SWAP', 'APPROVED'))
        self.choice = StubClient()(self.prompt)

    def test_post_cleanup_checks_cap_startup_floor_and_model_health(self):
        cases = [(sample(16), {'models': []}, True),
                 (sample(16 + 1 / 1024), {'models': []}, False),
                 (sample(0.6171875, 3.5 * guard.GIB), {'models': []}, False),
                 (sample(), {'models': [{'name': 'foreign', 'size_vram': 0}]}, False)]
        for value, loaded, passes in cases:
            with self.subTest(value=value, passes=passes), \
                 patch.object(guard.sys, 'platform', 'linux'), \
                 patch.object(guard, 'inspect_standalone', return_value={'model_digests': {MODEL: MODEL_DIGEST}}), \
                 patch.object(guard, 'generate', return_value={'code': self.choice, 'metrics': {}, 'wall_seconds': 0.1, 'resource_samples': []}) as generated, \
                 patch.object(guard, 'local_resources', return_value=value), \
                 patch.object(guard, 'get_json', return_value=loaded), \
                 patch('local_model.time.sleep'):
                client = GraniteClient(lock_path=self.lock, standalone_demo=True)
                client.attest_standalone('RUN')
                if passes:
                    client(self.prompt)
                    self.assertTrue(client.execution_verified)
                    self.assertTrue(client.last_run['cleanup_confirmed'])
                else:
                    with self.assertRaises(LocalModelHeld):
                        client(self.prompt)
                    self.assertFalse(client.execution_verified)
                with self.assertRaises(LocalModelHeld):
                    client(self.prompt)
                self.assertEqual(generated.call_count, 1)
                self.assertEqual(client.last_run['swap_policy']['max_swap_mib'], 16)
                self.assertIs(generated.call_args.kwargs['standalone_demo'], True)

    def run_monitored_client(self, swap):
        closed, admitted = threading.Event(), threading.Event()
        choice = self.choice
        over = swap > 16
        class Socket:
            def shutdown(self, _how):
                closed.set()
        class Response(io.BytesIO):
            status = 200
        class Connection:
            sock = Socket()
            calls = []
            def __init__(self, *args, **kwargs):
                pass
            def request(self, method, path, body, **kwargs):
                Connection.calls.append((method, path, json.loads(body)))
            def getresponse(self):
                if over:
                    if not closed.wait(5):
                        raise AssertionError('monitor did not cancel')
                    raise http.client.RemoteDisconnected('private provider text')
                if not admitted.wait(5):
                    raise AssertionError('monitor did not admit bounded swap')
                return Response((json.dumps({'response': choice, 'done': True}) + '\n').encode())
            def close(self):
                closed.set()
        real_guard = guard.guard
        def observed_guard(*args, **kwargs):
            result = real_guard(*args, **kwargs)
            if kwargs.get('own_model') == MODEL:
                admitted.set()
            return result
        replies = {'/api/version': {'version': 'fixture'},
                   '/api/tags': {'models': [{'name': MODEL, 'digest': MODEL_DIGEST}]},
                   '/api/ps': {'models': []}}
        readings = iter([sample(), sample(swap), sample()])
        with patch.object(guard.sys, 'platform', 'linux'), \
             patch.object(guard, 'local_resources', side_effect=lambda: next(readings)), \
             patch.object(guard, 'get_json', side_effect=lambda route: replies[route]), \
             patch.object(guard, 'guard', side_effect=observed_guard), \
             patch.object(guard.http.client, 'HTTPConnection', Connection), \
             patch('local_model.time.sleep'):
            client = GraniteClient(lock_path=self.lock, standalone_demo=True)
            client.attest_standalone('RUN')
            if over:
                with self.assertRaises(LocalModelHeld):
                    client(self.prompt)
                self.assertEqual(client.last_run['error_code'], 'SWAP_NONZERO')
                self.assertEqual(client.last_run['failure_evidence']['last_observation']['swap_used_mib'], swap)
                self.assertFalse(client.execution_verified)
                self.assertNotIn('private provider text', json.dumps(client.last_run))
            else:
                client(self.prompt)
                self.assertTrue(client.execution_verified)
                self.assertEqual(client.last_run['swap_used_mib_max'], swap)
            with self.assertRaises(LocalModelHeld):
                client(self.prompt)
        self.assertTrue(client.last_run['cleanup_confirmed'])
        self.assertEqual(len(Connection.calls), 1)
        self.assertEqual(Connection.calls[0][:2], ('POST', '/api/generate'))
        self.assertEqual(Connection.calls[0][2]['keep_alive'], 0)
        self.assertEqual(Connection.calls[0][2]['options'], guard.GENERATION_OPTIONS)

    def test_monitored_small_allocation_allows_one_mock_request(self):
        self.run_monitored_client(0.6171875)

    def test_crossing_cap_cancels_one_mock_request_and_preserves_reason(self):
        self.run_monitored_client(16 + 1 / 1024)


class SwapPolicyReportingTests(unittest.TestCase):
    def test_startup_consent_uses_current_policy_without_internal_branding(self):
        from app import _confirm_standalone_demo
        for platform, expected in (('linux', 'at most 16 MiB allocated swap'),
                                   ('darwin', 'zero swap')):
            class Client:
                standalone_demo = True
                def attest_standalone(self, answer):
                    return answer == 'RUN'
                def preflight(self):
                    return {'resources': sample(), 'cached_models': [MODEL],
                            'model_digests': {MODEL: MODEL_DIGEST}}
            output = io.StringIO()
            with self.subTest(platform=platform), patch.object(guard.sys, 'platform', platform), \
                 patch('builtins.input', return_value='RUN'), contextlib.redirect_stdout(output):
                self.assertTrue(_confirm_standalone_demo(Client()))
            self.assertIn(expected, output.getvalue())
            self.assertNotIn('Second Brain', output.getvalue())
            self.assertNotIn('Controller', output.getvalue())
            self.assertNotIn('zero-swap check passed', output.getvalue())

    @unittest.skipUnless(__import__('private_file_support').supports_private_files(),
                         'POSIX owner-private filesystem behavior unavailable')
    def test_private_journal_reports_linux_cap_instead_of_zero_swap(self):
        import test_judge_sandbox_flow as fixture
        flow = fixture.FlowTests()
        flow.setUp()
        self.addCleanup(flow.tearDown)
        client = StubClient()
        client.standalone_demo = True
        with patch.object(guard.sys, 'platform', 'linux'):
            flow.run_flow(flow.successful_transport(), client=client)
        model = flow.journal_data()['model']
        self.assertEqual(model['guardPolicy'], 'linux_standalone_small_swap')
        self.assertEqual(model['maxSwapMiB'], 16)


if __name__ == '__main__':
    unittest.main()
