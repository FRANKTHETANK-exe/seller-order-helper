"""Saved receipt integration, with fixture evidence and no live dependencies."""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import app
from local_model import StubClient
from reviewer_sandbox_order import write_order_pin
import test_reviewer_sandbox_order as fixtures


class RecordingStub(StubClient):
    def __call__(self, prompt):
        self.prompt = prompt
        return super().__call__(prompt)


class SavedSandboxReviewTests(unittest.TestCase):
    def fixture(self, directory):
        parent = fixtures.make_private_dir(Path(directory))
        pin_path, journal_path = parent / 'order.json', parent / 'attempt.json'
        pin = write_order_pin(pin_path, fixtures.ORDER_ID, configuration_id=fixtures.CONFIG_ID)
        fixtures.ReviewerOrderPinTests.write_successful_journal(
            journal_path, order_id=pin.order_id, configuration_id=pin.configuration_id)
        return pin_path, journal_path

    def args(self, pin, journal):
        return ['--client', 'stub', 'sandbox-ai-review', '--saved-reviewer-order',
                '--reviewer-order-pin', str(pin), '--reviewer-order-journal', str(journal)]

    @unittest.skipUnless(__import__("private_file_support").supports_private_files(), "POSIX owner-private filesystem behavior unavailable")
    def test_saved_facts_reach_selector_keep_historical_label_and_preserve_evidence(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            pin, journal = self.fixture(temp)
            before = (pin.read_bytes(), journal.read_bytes())
            for target in ('sandbox_adapter.UrllibSandboxTransport.request',
                           'sandbox_adapter.load_owner_sandbox_credentials',
                           'runtime_guard.generate', 'runtime_guard.get_json'):
                stack.enter_context(patch(target, side_effect=AssertionError('Unexpected live call')))
            client = RecordingStub()
            output = StringIO()
            with redirect_stdout(output), patch('builtins.input', return_value=''):
                self.assertEqual(app.main(self.args(pin, journal), client=client), 0)
            prompt = json.loads(client.prompt)
            self.assertEqual(prompt['facts']['order_id'], fixtures.ORDER_ID)
            self.assertEqual(prompt['facts']['source'], 'saved_paypal_sandbox_receipt')
            self.assertEqual(prompt['facts']['observed_at'], '2026-10-31T02:48:35Z')
            self.assertNotIn(str(pin), client.prompt)
            self.assertNotIn(fixtures.CONFIG_ID, client.prompt)
            from reviewer_sandbox_order import active_reviewer_pin_path
            self.assertIsNone(active_reviewer_pin_path())
            text = output.getvalue()
            self.assertIn('saved receipt evidence; no API request made.', text)
            self.assertIn('The PayPal check saved on 2026-10-31', text)
            self.assertIn('NO MODEL INFERENCE', text)
            self.assertIn('HUMAN REVIEW PENDING', text)
            self.assertNotIn(fixtures.ORDER_ID, text)
            self.assertEqual((pin.read_bytes(), journal.read_bytes()), before)

    @unittest.skipUnless(__import__("private_file_support").supports_private_files(), "POSIX owner-private filesystem behavior unavailable")
    def test_uncertain_or_mismatched_evidence_stops_before_client_construction(self):
        for mutation in ('pending_create', 'wrong_identity', 'naive_timestamp'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                pin, journal = self.fixture(temp)
                value = json.loads(journal.read_text())
                if mutation == 'pending_create':
                    value['calls'][1]['outcome'] = 'pending'
                elif mutation == 'wrong_identity':
                    value['orderId'] = 'DIFFERENTORDER123'
                else:
                    value['orderObservedAt'] = '2026-10-31T02:48:35'
                journal.write_text(json.dumps(value))
                with patch('app._make_client') as create_client, redirect_stdout(StringIO()):
                    self.assertEqual(app.main(self.args(pin, journal)), 2)
                    create_client.assert_not_called()

    def test_missing_explicit_journal_is_rejected(self):
        with patch('app._make_client') as create_client, redirect_stdout(StringIO()):
            self.assertEqual(app.main(['sandbox-ai-review', '--saved-reviewer-order']), 2)
            create_client.assert_not_called()

    def test_paths_without_saved_mode_are_rejected(self):
        with patch('app._make_client') as create_client, redirect_stdout(StringIO()):
            self.assertEqual(app.main(['sandbox-ai-review', '--reviewer-order-pin', '/tmp/example.json']), 2)
            create_client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
