"""Simulated frontend DOM/transport checks, not rendered-browser or screen-reader evidence."""
import shutil
import subprocess
import unittest
from pathlib import Path

@unittest.skipUnless(shutil.which('node'), 'Optional Node frontend harness unavailable')
class SessionRecoveryTests(unittest.TestCase):
    def check(self, scenario):
        result = subprocess.run(['node', 'session-recovery.test.cjs', scenario], cwd=Path(__file__).parent, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PASS '+scenario, result.stdout)
    def test_pending_initial_state_locks_actions(self): self.check('initial-pending')
    def test_failed_initial_state_read_can_be_reloaded(self): self.check('initial-unavailable')
    def test_lost_generation_response_is_reconciled_without_replay(self): self.check('generate-lost')
    def test_lost_review_response_preserves_acknowledged_unsent_state(self): self.check('generate-lost-review-lost')
    def test_lost_state_read_locks_actions_until_explicit_recovery(self): self.check('generate-lost-state-unavailable')
    def test_refresh_failure_diagnostics_survive_without_repeating_refresh(self): self.check('refresh-lost')
    def test_stale_live_stub_provenance_remains_live_and_historical(self): self.check('stale-live-stub')
    def test_stale_live_granite_provenance_remains_live_and_historical(self): self.check('stale-live-granite')
