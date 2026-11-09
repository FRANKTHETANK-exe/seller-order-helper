"""Focused startup fixtures only: no credentials, provider, health or inference calls."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
try:
    import fcntl
except ImportError:
    fcntl = None
import getpass
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import warnings

import app
import judge_sandbox_flow as judge
import local_model
import prepare_paypal_sandbox_env as setup
import runtime_guard as guard


SUPPORTED_POSIX = fcntl is not None and sys.platform in ('darwin', 'linux')
requires_posix = unittest.skipUnless(SUPPORTED_POSIX, 'Fixture requires supported POSIX')


class StartupReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.lock = self.root / 'model.lock'
        self.client = (local_model.GraniteClient(lock_path=self.lock, standalone_demo=True)
                       if SUPPORTED_POSIX else None)
        self.checked = {'cached_models': [local_model.MODEL],
                        'model_digests': {local_model.MODEL: local_model.MODEL_DIGEST},
                        'resources': {'memory_pressure_free_percent': 75}}
        # An unmocked contact is a test failure, never a real service call.
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target in ('socket.create_connection', 'socket.socket.connect',
                       'runtime_guard.generate', 'judge_sandbox_flow.load_owner_sandbox_credentials'):
            self.stack.enter_context(patch(target, side_effect=AssertionError('no live calls permitted')))

    def assert_lock_refused(self):
        with patch.object(guard, 'inspect_standalone') as inspect:
            with self.assertRaises(local_model.LocalModelHeld) as held:
                self.client.check_startup_lock()
            self.assertEqual(held.exception.failure_code, 'ADMISSION_LOCK_FAILED')
            self.assertFalse(held.exception.request_attempted)
            inspect.assert_not_called()

    @requires_posix
    def test_probe_creates_writable_lock_and_releases_before_inspection(self):
        def inspect(_):
            with self.lock.open('a+') as other:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self.checked
        with patch.object(guard, 'inspect_standalone', side_effect=inspect):
            for _ in range(2):
                self.client.check_startup_lock()
                self.assertEqual(self.client.preflight(), self.checked)
        self.assertEqual(self.lock.read_bytes(), b'')
        self.assertFalse(self.client._used)

    @requires_posix
    def test_existing_lock_contents_and_permissions_are_preserved(self):
        self.lock.write_text('preserve')
        self.lock.chmod(0o640)
        with patch.object(guard, 'inspect_standalone', return_value=self.checked):
            self.client.check_startup_lock()
        self.assertEqual(self.lock.read_text(), 'preserve')
        self.assertEqual(stat.S_IMODE(self.lock.stat().st_mode), 0o640)

    @requires_posix
    def test_busy_lock_is_refused_without_unlocking_other_holder(self):
        with self.lock.open('a+') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_lock_refused()
            with self.lock.open('a+') as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @requires_posix
    def test_read_only_lock_is_refused_without_permission_repair(self):
        self.lock.write_text('preserve')
        self.lock.chmod(0o400)
        self.addCleanup(self.lock.chmod, 0o600)
        self.assert_lock_refused()
        self.assertEqual(stat.S_IMODE(self.lock.stat().st_mode), 0o400)

    @requires_posix
    def test_read_only_data_directory_is_refused_without_creating_lock(self):
        directory = self.root / 'data'
        directory.mkdir(mode=0o500)
        self.addCleanup(directory.chmod, 0o700)
        self.client.lock_path = directory / 'model.lock'
        self.assert_lock_refused()
        self.assertFalse(self.client.lock_path.exists())
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o500)

    @requires_posix
    def test_linked_and_dangling_lock_paths_are_refused(self):
        target = self.root / 'target'
        for exists in (False, True):
            with self.subTest(existing_target=exists):
                if exists:
                    target.write_text('preserve')
                self.lock.symlink_to(target)
                self.assert_lock_refused()
                self.assertTrue(self.lock.is_symlink())
                self.lock.unlink()  # Fixture cleanup only.
        self.assertEqual(target.read_text(), 'preserve')

    @requires_posix
    def test_linked_data_directory_is_refused(self):
        target = self.root / 'real-data'
        target.mkdir()
        linked = self.root / 'linked-data'
        linked.symlink_to(target, target_is_directory=True)
        self.client.lock_path = linked / 'model.lock'
        self.assert_lock_refused()
        self.assertFalse((target / 'model.lock').exists())

    @requires_posix
    def test_hard_link_and_fifo_are_refused(self):
        target = self.root / 'target'
        target.write_text('preserve')
        os.link(target, self.lock)
        self.assert_lock_refused()
        self.lock.unlink()  # Fixture cleanup only.
        os.mkfifo(self.lock)
        self.assert_lock_refused()
        self.assertEqual(target.read_text(), 'preserve')

    @requires_posix
    def test_inference_time_preflight_avoids_self_conflict_but_rechecks_resources(self):
        with self.lock.open('a+') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(guard, 'inspect_standalone', return_value=self.checked) as inspect:
                self.assertEqual(self.client.preflight(), self.checked)
                inspect.assert_called_once_with({local_model.MODEL: local_model.MODEL_DIGEST})
            self.assert_lock_refused()

    @requires_posix
    def test_preflight_resource_refusal_does_not_leak_probe_lock(self):
        with patch.object(guard, 'inspect_standalone', side_effect=guard.PilotStop('fixture')):
            self.client.check_startup_lock()
            with self.assertRaises(guard.PilotStop):
                self.client.preflight()
        with self.lock.open('a+') as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @requires_posix
    def test_busy_lock_stops_judge_before_provider_or_credentials(self):
        for standalone in (False, True):
            with self.subTest(standalone=standalone):
                stdout, stderr = io.StringIO(), io.StringIO()
                answers = ['Has this $10 order been paid?', judge.CONFIRMATION]
                args = ['--run', '--reviewer-order-pin', str(self.root / 'pin.json'),
                        '--journal', str(self.root / 'journal.json')]
                if standalone:
                    args.append('--standalone')
                    answers.append('RUN')
                with self.lock.open('a+') as holder:
                    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with patch.object(app, '_make_client', return_value=self.client), \
                         patch('builtins.input', side_effect=answers), \
                         patch.object(judge, 'run_once') as run, \
                         patch.object(judge, 'UrllibSandboxTransport') as transport, \
                         patch.object(guard, 'get_json') as health, \
                         redirect_stdout(stdout), redirect_stderr(stderr):
                        result = judge.main(args)
                self.assertEqual(result, 2)
                run.assert_not_called()
                transport.assert_not_called()
                health.assert_not_called()
                self.assertIn('ADMISSION_LOCK_FAILED', stdout.getvalue() + stderr.getvalue())
                self.assertFalse((self.root / 'pin.json').exists())
                self.assertFalse((self.root / 'journal.json').exists())

    @requires_posix
    def test_confirmation_and_time_bounded_attestation_are_unchanged(self):
        for answer in ('', 'run', 'YES'):
            with self.subTest(answer=answer), patch('builtins.input', return_value=answer), \
                 patch.object(self.client, 'preflight') as preflight, redirect_stdout(io.StringIO()):
                self.assertFalse(app._confirm_standalone_demo(self.client))
                preflight.assert_not_called()
        with patch('local_model.time.monotonic', return_value=100):
            self.assertTrue(self.client.attest_standalone('RUN'))
        self.assertEqual(self.client._standalone_attested_until, 700)

    def test_allowlisted_diagnostics_never_render_exception_text_or_paths(self):
        client = SimpleNamespace(standalone_demo=True, attest_standalone=lambda _: True, preflight=lambda: None)
        codes = ('ADMISSION_LOCK_FAILED', 'ADMISSION_CHECK_FAILED', 'MODEL_TAG_LOOKUP_FAILED',
                 'MODEL_TAG_UNAVAILABLE', 'MODEL_DIGEST_UNAVAILABLE', 'MODEL_DIGEST_MISMATCH',
                 'RESOURCE_ADMISSION_DENIED', 'SWAP_NONZERO', 'SWAP_BASELINE_INVALID',
                 'SWAP_BASELINE_DRIFT', 'MODEL_PLATFORM_UNSUPPORTED', 'MODEL_SETUP_ERROR')
        for code in (*codes, 'UNTRUSTED /private/secret', ['unhashable']):
            with self.subTest(code=code):
                error = guard.PilotStop('fixture-secret /private/credential.env', failure_code=code)
                out = io.StringIO()
                with patch('builtins.input', return_value='RUN'), \
                     patch.object(client, 'preflight', side_effect=error), redirect_stdout(out):
                    self.assertFalse(app._confirm_standalone_demo(client))
                text = out.getvalue()
                expected = code if isinstance(code, str) and code in codes else 'MODEL_SETUP_ERROR'
                self.assertIn(expected + ':', text)
                self.assertNotIn('fixture-secret', text)
                self.assertNotIn('/private/', text)
                self.assertLess(len(text), 1100)

    def test_unclassified_exception_has_fixed_fallback(self):
        message = app._standalone_preflight_remedy(RuntimeError('fixture-secret /private/path'))
        self.assertTrue(message.startswith('MODEL_SETUP_ERROR:'))
        self.assertNotIn('fixture-secret', message)
        self.assertNotIn('/private/', message)

    def mocked_standalone(self, *, failing_route=None, models=None, resource_error=None):
        self.stack.enter_context(patch.object(guard.sys, 'platform', 'darwin'))
        def health(route):
            if route == failing_route:
                raise OSError('fixture-secret /private/path')
            return {'/api/version': {'version': 'fixture'}, '/api/tags': {'models': models or []},
                    '/api/ps': {'models': []}}[route]
        self.stack.enter_context(patch.object(guard, 'get_json', side_effect=health))
        self.stack.enter_context(patch.object(guard, 'local_resources', side_effect=resource_error,
                                            return_value={'fixture': True}))
        self.stack.enter_context(patch.object(guard, 'guard'))

    def test_missing_ollama_version_has_bounded_code(self):
        self.mocked_standalone(failing_route='/api/version')
        with self.assertRaises(guard.PilotStop) as held:
            guard.inspect_standalone({local_model.MODEL: local_model.MODEL_DIGEST})
        self.assertEqual(held.exception.failure_code, 'ADMISSION_CHECK_FAILED')
        self.assertNotIn('fixture-secret', str(held.exception))

    def test_loaded_model_lookup_failure_has_bounded_code(self):
        self.mocked_standalone(failing_route='/api/ps')
        with self.assertRaises(guard.PilotStop) as held:
            guard.inspect_standalone({local_model.MODEL: local_model.MODEL_DIGEST})
        self.assertEqual(held.exception.failure_code, 'ADMISSION_CHECK_FAILED')

    def test_resource_measurement_failure_has_bounded_code(self):
        self.mocked_standalone(resource_error=OSError('fixture-secret /private/path'))
        with self.assertRaises(guard.PilotStop) as held:
            guard.inspect_standalone({local_model.MODEL: local_model.MODEL_DIGEST})
        self.assertEqual(held.exception.failure_code, 'RESOURCE_ADMISSION_DENIED')

    def test_swap_refusal_code_is_preserved(self):
        self.mocked_standalone(resource_error=guard.PilotStop('fixture', failure_code='SWAP_NONZERO'))
        with self.assertRaises(guard.PilotStop) as held:
            guard.inspect_standalone({local_model.MODEL: local_model.MODEL_DIGEST})
        self.assertEqual(held.exception.failure_code, 'SWAP_NONZERO')

    def test_missing_model_and_digest_mismatch_keep_distinct_codes(self):
        for models, code in (([], 'MODEL_TAG_UNAVAILABLE'),
                             ([{'name': local_model.MODEL, 'digest': 'wrong'}], 'MODEL_DIGEST_MISMATCH')):
            with self.subTest(code=code), patch.object(guard.sys, 'platform', 'darwin'), \
                    patch.object(guard, 'get_json', side_effect=[
                    {'version': 'fixture'}, {'models': models}, {'models': []}]), \
                    patch.object(guard, 'local_resources', return_value={}):
                with self.assertRaises(guard.PilotStop) as held:
                    guard.inspect_standalone({local_model.MODEL: local_model.MODEL_DIGEST})
                self.assertEqual(held.exception.failure_code, code)

    def test_getpass_warning_aborts_before_either_credential_can_echo_or_write(self):
        target = self.root / 'fixture.env'
        for failing_prompt in (1, 2):
            with self.subTest(failing_prompt=failing_prompt):
                calls = []
                def hidden(prompt):
                    calls.append(prompt)
                    if len(calls) == failing_prompt:
                        return getpass.fallback_getpass(prompt, stream=io.StringIO())
                    return 'synthetic-fixture-id'
                out = io.StringIO()
                with patch.object(setup, 'TARGET', target), \
                     patch.object(setup, '_verify_parent'), \
                     patch.object(getpass, 'getpass', side_effect=hidden), \
                     patch.object(getpass, '_raw_input') as echoed_input, \
                     patch.object(setup, 'create_env_file') as write, \
                     warnings.catch_warnings(), redirect_stderr(out):
                    warnings.simplefilter('ignore', getpass.GetPassWarning)
                    self.assertEqual(setup.main(), 1)
                echoed_input.assert_not_called()
                write.assert_not_called()
                self.assertFalse(target.exists())
                self.assertEqual(len(calls), failing_prompt)
                self.assertIn('hidden input is unavailable', out.getvalue())
                self.assertNotIn('synthetic-fixture-id', out.getvalue())

    @requires_posix
    def test_successful_hidden_fixture_input_retains_private_file_contract(self):
        target = self.root / 'fixture.env'
        with patch.object(setup, 'TARGET', target), \
             patch.object(getpass, 'getpass', side_effect=['synthetic-id', 'synthetic-secret']), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(setup.main(), 0)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertIn('PAYPAL_SANDBOX_CLIENT_ID="synthetic-id"', target.read_text())
        self.assertNotIn('synthetic-secret', out.getvalue())

    @requires_posix
    def test_command_syntax_and_missing_python_help_exit(self):
        command = Path(__file__).resolve().parent / 'START_SANDBOX.command'
        syntax = subprocess.run(['/bin/sh', '-n', str(command)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'dirname').symlink_to('/usr/bin/dirname')
        # Explicit --help only, in a subprocess PATH fixture. Never default launch.
        result = subprocess.run(['/bin/sh', str(command), '--help'],
                                env={**os.environ, 'PATH': str(bin_dir)},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Python 3.10+', result.stderr)
        self.assertIn('https://www.python.org/downloads/macos/', result.stderr)
        self.assertNotIn('REAL CONNECTED MODE', result.stdout)


if __name__ == '__main__':
    unittest.main()
