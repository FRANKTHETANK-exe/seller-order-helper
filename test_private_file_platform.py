"""Simulated unsupported private-file capability, not a real Windows test."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import private_file_support
from judge_sandbox_flow import OneShotJournal, run_once
from prepare_paypal_sandbox_env import SetupError, create_env_file
from reviewer_sandbox_order import read_order_pin, write_order_pin
from sandbox_adapter import AdapterError, load_owner_sandbox_credentials
from sandbox_reconciliation import read_private_current_order


class UnsupportedPrivateFileTests(unittest.TestCase):
    def test_capability_rejects_windows_and_missing_posix_apis(self):
        with patch.object(private_file_support.os, 'name', 'nt'):
            self.assertFalse(private_file_support.supports_private_files())
        with patch.object(private_file_support.os, 'name', 'posix'), \
             patch.object(private_file_support.os, 'getuid', None, create=True):
            self.assertFalse(private_file_support.supports_private_files())
        with patch.object(private_file_support.os, 'name', 'posix'), \
             patch.object(private_file_support.os, 'fchmod', None, create=True):
            self.assertFalse(private_file_support.supports_private_files())

    def test_private_operations_refuse_before_any_file_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'not-created'
            with patch('reviewer_sandbox_order.supports_private_files', return_value=False), \
                 patch('judge_sandbox_flow.supports_private_files', return_value=False), \
                 patch('sandbox_adapter.supports_private_files', return_value=False), \
                 patch('sandbox_reconciliation.supports_private_files', return_value=False), \
                 patch('prepare_paypal_sandbox_env.supports_private_files', return_value=False), \
                 patch.object(Path, 'lstat', side_effect=AssertionError('unexpected filesystem read')):
                for operation in (lambda: write_order_pin(path, 'TESTORDER12345678'),
                                  lambda: read_order_pin(path),
                                  lambda: OneShotJournal(path, request_id='fixture').create(),
                                  lambda: load_owner_sandbox_credentials(path),
                                  lambda: read_private_current_order(profile_path=path)):
                    with self.subTest(operation=operation):
                        with self.assertRaises(AdapterError) as error:
                            operation()
                        self.assertEqual(error.exception.failure_code, 'PRIVATE_FILES_UNSUPPORTED')
                with self.assertRaises(SetupError):
                    create_env_file(path, 'fixture-id', 'fixture-secret')
            self.assertFalse(path.exists())

    def test_judge_flow_stops_before_confirmation_credentials_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            pin = Path(tmp) / 'pin'; journal = Path(tmp) / 'journal'
            def unexpected():
                self.fail('unsupported flow must not call confirmation or credential loader')
            with patch('judge_sandbox_flow.supports_private_files', return_value=False):
                with self.assertRaises(AdapterError) as error:
                    run_once(transport=object(), credentials=unexpected, granite_client=object(),
                             question='Has my payment gone through?', confirm_create=unexpected,
                             journal_path=journal, pin_path=pin)
            self.assertEqual(error.exception.failure_code, 'PRIVATE_FILES_UNSUPPORTED')
            self.assertFalse(pin.exists()); self.assertFalse(journal.exists())


if __name__ == '__main__':
    unittest.main()
