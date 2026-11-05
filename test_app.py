"""Offline app checks. Test approval flags exercise mechanics, not human review.

Every database is disposable. Clients are injected; no Granite or network call
is performed and no retained demonstration suggestion is approved by tests.
"""
from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import APP_ID, DB_VERSION, AppError, Store, main


class RecordingClient:
    kind = 'stub'
    model_name = 'offline-test-stub'
    execution_verified = False

    def __init__(self, *, callback=None, phrase='status_first', style='concise', malformed=False):
        self.calls = 0
        self.prompts = []
        self.callback = callback
        self.phrase = phrase
        self.style = style
        self.malformed = malformed
        self.last_run = {}

    def __call__(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        if self.callback:
            self.callback()
        facts = json.loads(prompt)['facts']
        self.last_run = {'status': 'stub_completed', 'model': self.model_name,
                         'inference_requests_attempted': 0, 'wall_seconds': 0,
                         'metrics': {'eval_count': 0}, 'limits': {'num_ctx': 4096},
                         'raw_prompt': 'must not persist', 'raw_output': 'must not persist'}
        if self.malformed:
            return '{"reply_text":"unapproved arbitrary wording"}'
        return json.dumps({'order_id': facts['order_id'], 'category': facts['category'],
                           'phrase_id': self.phrase, 'style_id': self.style})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'simulation-v3.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.order_id = self.store.create_order()['id']

    def draft(self, client=None):
        return self.store.draft_reply(self.order_id, client or RecordingClient())['draft']

    def approve(self, draft):
        return self.store.review_draft(self.order_id, approve=True, expected_digest=draft['content_digest'])

    def test_fixed_facts_and_version(self):
        order = self.store.get_order(self.order_id)
        self.assertRegex(order['id'], r'^SIM-[0-9A-F]{12}$')
        self.assertEqual(order['mode'], 'SIMULATED')
        self.assertEqual(order['source'], 'SIMULATED_SANDBOX')
        self.assertEqual(order['seller_id'], 'DEMO_SELLER')
        self.assertEqual(order['product_id'], 'demo-notebook')
        self.assertEqual((order['amount'], order['currency']), ('10.00', 'USD'))
        self.assertEqual(self.store.conn.execute('PRAGMA application_id').fetchone()[0], APP_ID)
        self.assertEqual(self.store.conn.execute('PRAGMA user_version').fetchone()[0], DB_VERSION)

    def test_approved_is_pending_and_completed_has_matching_mock_capture(self):
        self.store.set_status(self.order_id, 'APPROVED')
        approved = self.draft()
        self.assertEqual(approved['category'], 'payment_pending')
        self.assertIn('Payment is not confirmed', approved['reply_text'])
        self.assertIsNone(self.store.get_order(self.order_id)['capture_status'])
        self.store.set_status(self.order_id, 'COMPLETED')
        completed = self.draft()
        self.assertEqual(completed['category'], 'payment_confirmed')
        self.assertEqual(self.store.get_order(self.order_id)['capture_status'], 'COMPLETED')
        self.assertIn('simulated payment', completed['reply_text'])

    def test_pending_and_reviewed_suggestion_reopen_exactly(self):
        client = RecordingClient(phrase='item_first', style='friendly')
        pending = self.store.draft_reply(self.order_id, client)
        draft = pending['draft']
        self.assertEqual((draft['phrase_id'], draft['style_id']), ('item_first', 'friendly'))
        self.assertIsNone(draft['reviewed_at'])
        self.assertFalse(draft['model_execution_verified'])
        self.assertEqual(draft['client_metadata']['inference_requests_attempted'], 0)
        self.assertNotIn('raw_prompt', draft['client_metadata'])
        self.assertNotIn('raw_output', draft['client_metadata'])
        with closing(Store(self.path)) as reopened:
            self.assertEqual(reopened.get_order(self.order_id), pending)
        reviewed = self.approve(draft)
        self.assertTrue(reviewed['draft']['reviewed_at'])
        self.assertEqual(reviewed['draft']['review_source'], 'local_operator_explicit')
        self.assertIn('Human review completed', reviewed['draft']['reviewed_text'])
        self.assertIn('no message has been sent', reviewed['draft']['reviewed_text'])
        self.assertEqual(reviewed['draft']['reply_text'], draft['reply_text'])
        with closing(Store(self.path)) as reopened:
            self.assertEqual(reopened.get_order(self.order_id), reviewed)

    def test_status_change_invalidates_even_same_category_review(self):
        first = self.draft()
        self.approve(first)
        current = self.store.set_status(self.order_id, 'APPROVED')
        self.assertEqual(current['revision'], 1)
        self.assertIsNone(current['draft'])
        with self.assertRaises(AppError):
            self.approve(first)
        replacement = self.draft()
        self.assertEqual(replacement['category'], first['category'])
        self.assertNotEqual(replacement['facts_fingerprint'], first['facts_fingerprint'])
        self.assertIsNone(replacement['reviewed_at'])

    def test_regeneration_resets_review_and_rejects_old_digest(self):
        first = self.draft()
        self.approve(first)
        replacement = self.draft()
        self.assertEqual(first['reply_text'], replacement['reply_text'])
        self.assertNotEqual(first['content_digest'], replacement['content_digest'])
        self.assertIsNone(replacement['reviewed_at'])
        with self.assertRaisesRegex(AppError, 'Suggestion changed'):
            self.approve(first)
        self.assertIsNone(self.store.get_order(self.order_id)['draft']['reviewed_at'])

    def test_review_requires_explicit_flag_and_exact_digest(self):
        draft = self.draft()
        for kwargs in ({}, {'approve': True}, {'approve': True, 'expected_digest': '0' * 64},
                       {'approve': 1, 'expected_digest': draft['content_digest']}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(AppError):
                    self.store.review_draft(self.order_id, **kwargs)
        self.assertIsNone(self.store.get_order(self.order_id)['draft']['reviewed_at'])

    def test_altered_pending_content_cannot_be_reviewed(self):
        draft = self.draft()
        self.store.conn.execute('UPDATE suggestions SET reply_text=reply_text || ? WHERE order_id=?',
                                (' Altered wording.', self.order_id))
        with self.assertRaisesRegex(AppError, 'Suggestion changed'):
            self.approve(draft)
        self.assertIsNone(self.store.get_order(self.order_id)['draft']['reviewed_at'])

    def test_altered_reviewed_content_is_not_reapproved(self):
        draft = self.draft()
        self.approve(draft)
        self.store.conn.execute('UPDATE suggestions SET reviewed_text=? WHERE order_id=?',
                                ('Altered reviewed content', self.order_id))
        with self.assertRaisesRegex(AppError, 'review content changed'):
            self.approve(draft)

    def test_review_rechecks_full_facts_even_without_revision_change(self):
        draft = self.draft()
        # Simulate an outside data edit retaining the same pending category.
        self.store.conn.execute("UPDATE orders SET status='APPROVED' WHERE id=?", (self.order_id,))
        with self.assertRaisesRegex(AppError, 'facts or content changed'):
            self.approve(draft)
        self.assertIsNone(self.store.get_order(self.order_id)['draft']['reviewed_at'])

    def test_generation_is_outside_transaction_and_rejects_status_race(self):
        with closing(Store(self.path)) as other:
            def mutate():
                self.assertFalse(self.store.conn.in_transaction)
                other.set_status(self.order_id, 'APPROVED')
            client = RecordingClient(callback=mutate)
            with self.assertRaisesRegex(AppError, 'Order changed during'):
                self.store.draft_reply(self.order_id, client)
        self.assertEqual(client.calls, 1)
        current = self.store.get_order(self.order_id)
        self.assertEqual(current['status'], 'APPROVED')
        self.assertIsNone(current['draft'])

    def test_generation_rejects_fact_change_without_revision_change(self):
        with closing(Store(self.path)) as other:
            def mutate():
                other.conn.execute("UPDATE orders SET status='APPROVED' WHERE id=?", (self.order_id,))
            with self.assertRaisesRegex(AppError, 'Order changed during'):
                self.store.draft_reply(self.order_id, RecordingClient(callback=mutate))
        self.assertEqual(self.store.get_order(self.order_id)['revision'], 0)
        self.assertIsNone(self.store.get_order(self.order_id)['draft'])

    def test_generation_rejects_revision_change_with_identical_facts(self):
        with closing(Store(self.path)) as other:
            def mutate():
                other.conn.execute('UPDATE orders SET revision=revision+1 WHERE id=?', (self.order_id,))
            with self.assertRaisesRegex(AppError, 'Order changed during'):
                self.store.draft_reply(self.order_id, RecordingClient(callback=mutate))
        self.assertIsNone(self.store.get_order(self.order_id)['draft'])

    def test_bad_model_selection_preserves_previous_suggestion_and_review(self):
        before = self.approve(self.draft())
        client = RecordingClient(malformed=True)
        with self.assertRaisesRegex(AppError, 'no automatic retry'):
            self.store.draft_reply(self.order_id, client)
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.store.get_order(self.order_id), before)

    def test_stub_cannot_claim_verified_real_execution(self):
        client = RecordingClient()
        client.execution_verified = True
        self.assertFalse(self.draft(client)['model_execution_verified'])

    def test_idempotent_status_preserves_current_draft_and_review(self):
        reviewed = self.approve(self.draft())
        self.assertEqual(self.store.set_status(self.order_id, 'CREATED'), reviewed)

    def test_invalid_transitions_and_missing_order_do_not_change_state(self):
        before = self.store.draft_reply(self.order_id, RecordingClient())
        for status in ('COMPLETED', 'created', None, True):
            with self.subTest(status=status):
                with self.assertRaises(AppError):
                    self.store.set_status(self.order_id, status)
                self.assertEqual(self.store.get_order(self.order_id), before)
        with self.assertRaises(AppError):
            self.store.get_order('SIM-MISSING')

    def test_voided_order_is_terminal_and_other_order_is_unchanged(self):
        other = self.store.create_order()
        self.store.set_status(self.order_id, 'VOIDED')
        draft = self.draft()
        self.assertEqual(draft['category'], 'order_voided')
        self.assertIn('marked voided', draft['reply_text'])
        with self.assertRaises(AppError):
            self.store.set_status(self.order_id, 'APPROVED')
        self.assertEqual(self.store.get_order(other['id']), other)
        self.assertEqual(len(self.store.list_orders()), 2)

    def test_v1_v2_and_unrelated_database_bytes_are_preserved(self):
        for version in (0, 1, 2):
            with self.subTest(version=version):
                path = Path(self.temp.name) / f'earlier-{version}.sqlite3'
                with closing(sqlite3.connect(path)) as conn:
                    conn.execute(f'PRAGMA application_id={APP_ID if version else 0}')
                    conn.execute(f'PRAGMA user_version={version}')
                    conn.execute('CREATE TABLE preserved (value TEXT)')
                    conn.execute('INSERT INTO preserved VALUES (?)', ('original',))
                    conn.commit()
                before = path.read_bytes()
                with self.assertRaisesRegex(AppError, 'no migration'):
                    Store(path)
                self.assertEqual(path.read_bytes(), before)

    def test_unrelated_file_and_link_are_preserved(self):
        text = Path(self.temp.name) / 'original.txt'
        text.write_text('preserve this original file')
        before = hashlib.sha256(text.read_bytes()).hexdigest()
        with self.assertRaises(AppError):
            Store(text)
        self.assertEqual(hashlib.sha256(text.read_bytes()).hexdigest(), before)
        linked = Path(self.temp.name) / 'link.sqlite3'
        linked.symlink_to(self.path)
        with self.assertRaisesRegex(AppError, 'linked'):
            Store(linked)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'cli.sqlite3'

    def run_cli(self, *args, client=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['--db', str(self.path), *args], client=client)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_demo_calls_client_once_leaves_pending_and_labels_every_line(self):
        client = RecordingClient()
        code, output, errors = self.run_cli('demo', client=client)
        self.assertEqual(code, 0, output + errors)
        self.assertEqual(client.calls, 1)
        self.assertTrue(all(line.startswith('[SIMULATED]') for line in output.splitlines()))
        self.assertIn('APPROVED never means paid', output)
        self.assertIn('--suggestion-digest', output)
        self.assertIn('PENDING; no automatic approval', output)
        self.assertIn('NEVER SENT', output)
        self.assertEqual(json.loads(client.prompts[0])['facts']['source'], 'SIMULATED_SANDBOX')
        with closing(Store(self.path)) as store:
            order = store.get_order(store.list_orders()[0]['id'])
            self.assertEqual((order['status'], order['capture_status']), ('COMPLETED', 'COMPLETED'))
            self.assertIsNone(order['draft']['reviewed_at'])

    def test_default_cli_selects_stub_and_show_does_not_construct_client(self):
        client = RecordingClient()
        with patch('app._make_client', return_value=client) as constructor:
            self.assertEqual(self.run_cli('demo')[0], 0)
            constructor.assert_called_once_with('stub')
        with closing(Store(self.path)) as store:
            order_id = store.list_orders()[0]['id']
        with patch('app._make_client', side_effect=AssertionError('must not construct client')):
            self.assertEqual(self.run_cli('show', order_id)[0], 0)
            self.assertEqual(self.run_cli('list')[0], 0)

    def test_review_flags_are_required_and_help_errors_are_labeled(self):
        self.run_cli('demo', client=RecordingClient())
        with closing(Store(self.path)) as store:
            order_id = store.list_orders()[0]['id']
            draft = store.get_order(order_id)['draft']
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            main(['--db', str(self.path), 'review', order_id])
        self.assertEqual(raised.exception.code, 2)
        self.assertTrue(all(line.startswith('[SIMULATED]') for line in errors.getvalue().splitlines()))
        code, output, _ = self.run_cli('review', order_id, '--approve', '--suggestion-digest', draft['content_digest'])
        self.assertEqual(code, 0, output)
        self.assertIn('Human review completed', output)
        self.assertIn('NEVER SENT', output)

    def test_reviewer_order_refresh_can_run_without_granite(self):
        client = RecordingClient()
        with patch('app._make_client', return_value=client), \
             patch('sandbox_review_ui.serve_review_ui') as serve:
            code, output, errors = self.run_cli('--client', 'stub', 'sandbox-review-ui',
                                                '--refresh-reviewer-order', '--port', '0')
        self.assertEqual(code, 0, output + errors)
        serve.assert_called_once()
        self.assertTrue(callable(serve.call_args.kwargs['refresh_known_order']))
        self.assertEqual(client.calls, 0)
        self.assertNotIn('requires --client granite', errors)

    def test_held_client_has_safe_message_and_no_retry(self):
        class HeldClient(RecordingClient):
            kind = 'granite'
            model_name = 'granite4.2:3b'

            def __call__(self, prompt):
                self.calls += 1
                self.last_run = {'status': 'held_before_request', 'reason': 'memory threshold reached'}
                raise RuntimeError('raw transport details must not display')
        client = HeldClient()
        code, output, _ = self.run_cli('--client', 'granite', 'demo', client=client)
        self.assertEqual(code, 2)
        self.assertEqual(client.calls, 1)
        self.assertIn('held or failed', output)
        self.assertIn('no automatic retry', output)
        self.assertNotIn('raw transport details', output)
        with closing(Store(self.path)) as store:
            self.assertIsNone(store.get_order(store.list_orders()[0]['id'])['draft'])


if __name__ == '__main__':
    unittest.main()
