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
    def test_lost_generation_response_is_reconciled_without_replay_or_review(self): self.check('generate-lost')
    def test_lost_state_read_locks_actions_until_explicit_recovery(self): self.check('generate-lost-state-unavailable')
    def test_refresh_failure_diagnostics_survive_without_repeating_refresh(self): self.check('refresh-lost')
    def test_refresh_success_unlocks_generation_and_preserves_live_provenance(self): self.check('refresh-success')
    def test_stale_live_stub_response_remains_visible_but_actions_lock(self): self.check('stale-live-stub')
    def test_stale_live_granite_response_remains_visible_but_actions_lock(self): self.check('stale-live-granite')
    def test_stale_order_blocks_first_generation(self): self.check('stale-before-generation')
    def test_initial_load_and_page_refresh_preserve_state_without_replay(self): self.check('initial-ready')
    def test_new_offline_question_is_explicit_and_versioned(self): self.check('new-question')
    def test_interrupted_new_question_is_reconciled_without_replay(self): self.check('new-question-lost')
    def test_failed_generation_has_optional_diagnostic_without_retry(self): self.check('generation-failed')
    def test_failed_stub_generation_never_claims_success(self): self.check('stub-generation-failed')
    def test_general_request_failure_does_not_claim_timeout(self): self.check('request-failed')
    def test_completed_run_shows_original_question_without_replay_or_review(self): self.check('completed-ready')
    def test_completed_run_marks_expired_order_without_new_actions(self): self.check('completed-stale')
    def test_successful_generation_displays_response_and_focuses_it(self): self.check('generate-success')
    def test_empty_result_never_claims_success(self): self.check('generation-empty-result')
    def test_repeated_clicks_during_generation_cannot_repeat_request(self): self.check('generate-pending')
    def test_empty_question_is_local_validation_only(self): self.check('empty-question')
    def test_saved_record_is_not_presented_as_current(self): self.check('saved-record')
    def test_utf8_size_rejection_keeps_question_and_explains_correction_without_replay(self): self.check('validation-rejected')
    def test_normalized_question_rejection_explains_correction_without_replay(self): self.check('normalized-question-rejected')
    def test_unknown_api_error_text_is_never_shown(self): self.check('unknown-api-error')
    def test_changed_question_is_explained_and_focus_restored_without_replay(self): self.check('stale-question-rejected')
    def test_changed_question_with_result_does_not_pair_it_with_old_input(self): self.check('stale-question-with-result')
    def test_changed_question_error_does_not_hide_resource_failure(self): self.check('stale-question-with-failure')
    def test_changed_question_without_result_does_not_claim_one_exists(self): self.check('stale-question-pending')
