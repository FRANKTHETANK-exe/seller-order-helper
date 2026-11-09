"""Offline UI/adapter reconciliation checks; all network calls are faked."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import main
import sandbox_reconciliation
from sandbox_adapter import (HttpResponse, OAUTH_TOKEN_URL, SANDBOX_BASE_URL,
                             AdapterError, SandboxCredentials)
from sandbox_reconciliation import KNOWN_ORDER_ID, KNOWN_REQUEST_ID, read_recorded_evidence, refresh_known_order


ROOT = Path(__file__).parent
RECEIPT = ROOT / 'evidence' / 'offline-synthetic-receipt.json'

PUBLIC_ORDER_ID = KNOWN_ORDER_ID
TEST_ORDER_ID = 'TESTORDER123'
TEST_LIVE_FACTS = {'order_id': TEST_ORDER_ID, 'order_status': 'CREATED',
    'capture_status': None, 'payment_truth': 'CREATED_NOT_APPROVED',
    'currency': 'USD', 'amount': '10.00', 'observed_at': '2026-10-30T12:00:00Z',
    'source': 'live_paypal_sandbox_read'}

def refresh_fake_order(**kwargs):
    # Fake transport tests use an explicit synthetic pin; the distributable
    # runtime and saved receipt remain redacted and fail closed.
    with patch.object(sandbox_reconciliation, 'KNOWN_ORDER_ID', TEST_ORDER_ID), \
         patch.object(sandbox_reconciliation, 'read_recorded_evidence',
                      return_value={**TEST_LIVE_FACTS, 'source':'saved_paypal_sandbox_receipt'}):
        return sandbox_reconciliation.refresh_known_order(**kwargs)


class FakeSandboxTransport:
    def __init__(self, order_status='CREATED', capture_status=None):
        self.requests = []
        self.order_status = order_status
        self.capture_status = capture_status

    def request(self, request):
        self.requests.append(request)
        if request.url == OAUTH_TOKEN_URL:
            return HttpResponse(200, json.dumps({'access_token': 'FAKE_TOKEN',
                'token_type': 'Bearer', 'expires_in': 3600}).encode())
        expected = SANDBOX_BASE_URL + '/v2/checkout/orders/' + TEST_ORDER_ID
        if request.method != 'GET' or request.url != expected or request.body is not None:
            return HttpResponse(400, b'{}')
        unit = {'amount': {'currency_code': 'USD', 'value': '10.00'},
                'items': [{'sku': 'demo-notebook', 'name': 'Demo notebook', 'quantity': '1',
                           'unit_amount': {'currency_code': 'USD', 'value': '10.00'}}]}
        if self.capture_status:
            unit['payments'] = {'captures': [{'status': self.capture_status,
                'amount': {'currency_code': 'USD', 'value': '10.00'}}]}
        body = {'id': TEST_ORDER_ID, 'status': self.order_status, 'purchase_units': [unit]}
        return HttpResponse(200, json.dumps(body).encode())


class InvalidAmountTransport(FakeSandboxTransport):
    def request(self, request):
        if request.url == OAUTH_TOKEN_URL:
            return super().request(request)
        self.requests.append(request)
        body = {'id': TEST_ORDER_ID, 'status': 'CREATED',
                'purchase_units': [{'amount': {'currency_code': 'USD', 'value': '9.99'}}]}
        return HttpResponse(200, json.dumps(body).encode())


class ReconciliationTests(unittest.TestCase):
    def test_public_candidate_keeps_runtime_pin_redacted(self):
        self.assertEqual(KNOWN_ORDER_ID, 'REDACTEDORDERID')

    def test_redacted_runtime_pin_fails_before_credentials_or_network(self):
        transport = FakeSandboxTransport()
        credential_calls = []
        with patch.object(sandbox_reconciliation, 'KNOWN_ORDER_ID', 'REDACTEDORDERID'):
            with self.assertRaises(AdapterError) as caught:
                sandbox_reconciliation.refresh_known_order(transport=transport,
                    credential_loader=lambda: credential_calls.append(True))
        error = caught.exception
        self.assertEqual((error.stage, error.failure_code),
                         ('configuration', 'ORDER_PIN_REDACTED'))
        self.assertEqual(error.safe_call_counts,
                         {'oauth_attempts': 0, 'pinned_order_get_attempts': 0})
        self.assertEqual(credential_calls, [])
        self.assertEqual(transport.requests, [])

    def test_saved_receipt_is_allowlisted_record_not_live_state(self):
        evidence = read_recorded_evidence(RECEIPT)
        self.assertEqual(evidence, {'order_id': KNOWN_ORDER_ID, 'order_status': 'CREATED',
            'capture_status': None, 'payment_truth': 'CREATED_NOT_APPROVED',
            'currency': 'USD', 'amount': '10.00', 'observed_at': '2026-01-01T00:00:00Z',
            'source': 'synthetic_order_fixture'})

    def test_receipt_rejects_mutated_order_id_and_post_counts(self):
        original = json.loads(RECEIPT.read_text())
        try:
            # Public-release placeholders must never be accepted as the private
            # runtime pin, even though they satisfy the generic ID syntax.
            for key, value in (('orderId', 'ANOTHERORDER'),
                               ('postCount', 2), ('capturePresent', True)):
                modified = dict(original); modified[key] = value
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'receipt.json'
                    path.write_text(json.dumps(modified))
                    with self.subTest(field=key), self.assertRaises(AdapterError):
                        read_recorded_evidence(path)
        finally:
            pass

    def test_live_refresh_is_one_oauth_then_one_get_and_no_write(self):
        transport = FakeSandboxTransport()
        result = refresh_fake_order(transport=transport,
            credential_loader=lambda: SandboxCredentials('fixture-id', 'fixture-secret'))
        self.assertEqual(result['payment_truth'], 'CREATED_NOT_APPROVED')
        self.assertEqual(result['order_status'], 'CREATED')
        self.assertTrue(result['read_only'])
        self.assertEqual([r.method for r in transport.requests], ['POST', 'GET'])
        self.assertEqual(transport.requests[0].url, OAUTH_TOKEN_URL)
        self.assertEqual(transport.requests[1].url,
                         SANDBOX_BASE_URL + '/v2/checkout/orders/' + TEST_ORDER_ID)
        self.assertNotIn(KNOWN_REQUEST_ID, repr(transport.requests))
        self.assertNotIn('FAKE_TOKEN', repr(transport.requests))
        self.assertRegex(result['observed_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(result['safe_call_counts'],
                         {'oauth_attempts': 1, 'pinned_order_get_attempts': 1})

    def test_order_validation_failure_identifies_branch_and_safe_attempt_counts(self):
        transport = InvalidAmountTransport()
        with self.assertRaises(AdapterError) as caught:
            refresh_fake_order(transport=transport,
                credential_loader=lambda: SandboxCredentials('fixture-id', 'private-secret-fixture'))
        error = caught.exception
        self.assertEqual((error.stage, error.failure_code),
                         ('order_validation', 'ORDER_AMOUNT_CURRENCY_MISMATCH'))
        self.assertEqual(error.safe_call_counts,
                         {'oauth_attempts': 1, 'pinned_order_get_attempts': 1})
        self.assertNotIn('private-secret-fixture', str(error))
        self.assertNotIn('9.99', str(error))
        self.assertEqual([r.method for r in transport.requests], ['POST', 'GET'])

    def test_order_http_rejection_preserves_status_and_attempt_counts(self):
        class RejectedOrderTransport(FakeSandboxTransport):
            def request(self, request):
                self.requests.append(request)
                if request.url == OAUTH_TOKEN_URL:
                    return HttpResponse(200, json.dumps({'access_token':'FAKE_TOKEN',
                        'token_type':'Bearer','expires_in':3600}).encode())
                return HttpResponse(403, b'private response body')
        transport = RejectedOrderTransport()
        with self.assertRaises(AdapterError) as caught:
            refresh_fake_order(transport=transport,
                credential_loader=lambda: SandboxCredentials('fixture-id', 'private-secret-fixture'))
        self.assertEqual((caught.exception.stage, caught.exception.failure_code,
                          caught.exception.http_status),
                         ('order_get_http', 'PINNED_ORDER_GET_REJECTED', 403))
        self.assertEqual(caught.exception.safe_call_counts,
                         {'oauth_attempts': 1, 'pinned_order_get_attempts': 1})
        self.assertNotIn('private response body', str(caught.exception))

    def test_safe_transport_error_classification_is_not_collapsed(self):
        class BrokenOrderTransport(FakeSandboxTransport):
            def request(self, request):
                if request.url == OAUTH_TOKEN_URL:
                    self.requests.append(request)
                    return HttpResponse(200, json.dumps({'access_token':'FAKE_TOKEN',
                        'token_type':'Bearer','expires_in':3600}).encode())
                self.requests.append(request)
                raise AdapterError('private transport detail', stage='transport',
                    failure_code='SANDBOX_TLS_FAILURE', underlying_exception_class='SSLError')
        transport = BrokenOrderTransport()
        with self.assertRaises(AdapterError) as caught:
            refresh_fake_order(transport=transport,
                credential_loader=lambda: SandboxCredentials('fixture-id', 'private-secret-fixture'))
        self.assertEqual((caught.exception.stage, caught.exception.failure_code,
                          caught.exception.underlying_exception_class),
                         ('transport', 'SANDBOX_TLS_FAILURE', 'SSLError'))
        self.assertEqual(caught.exception.safe_call_counts,
                         {'oauth_attempts': 1, 'pinned_order_get_attempts': 1})
        self.assertNotIn('private transport detail', str(caught.exception))

    def test_credential_validation_failure_proves_no_transport_attempt(self):
        transport = FakeSandboxTransport()
        def reject_credentials():
            raise AdapterError('fixture credential rejection')
        with self.assertRaises(AdapterError) as caught:
            refresh_fake_order(transport=transport, credential_loader=reject_credentials)
        self.assertEqual((caught.exception.stage, caught.exception.failure_code),
                         ('credential_validation', 'SANDBOX_CREDENTIAL_INVALID'))
        self.assertEqual(caught.exception.safe_call_counts,
                         {'oauth_attempts': 0, 'pinned_order_get_attempts': 0})
        self.assertEqual(transport.requests, [])

    def test_oauth_rejection_proves_order_get_was_not_attempted(self):
        class OAuthRejectingTransport(FakeSandboxTransport):
            def request(self, request):
                self.requests.append(request)
                return HttpResponse(401, b'')
        transport = OAuthRejectingTransport()
        with self.assertRaises(AdapterError) as caught:
            refresh_fake_order(transport=transport,
                credential_loader=lambda: SandboxCredentials('fixture-id', 'private-secret-fixture'))
        self.assertEqual((caught.exception.stage, caught.exception.failure_code),
                         ('oauth_http', 'OAUTH_HTTP_REJECTED'))
        self.assertEqual(caught.exception.safe_call_counts,
                         {'oauth_attempts': 1, 'pinned_order_get_attempts': 0})
        self.assertEqual([request.method for request in transport.requests], ['POST'])

    def test_truth_distinguishes_approved_and_capture_states(self):
        for status, capture, truth in (
            ('APPROVED', None, 'APPROVED_WITHOUT_CAPTURE_EVIDENCE'),
            ('COMPLETED', None, 'COMPLETED_WITHOUT_CAPTURE_EVIDENCE'),
            ('COMPLETED', 'PENDING', 'CAPTURE_PENDING'),
            ('COMPLETED', 'COMPLETED', 'CAPTURED'),
        ):
            with self.subTest(status=status, capture=capture):
                result = refresh_fake_order(transport=FakeSandboxTransport(status, capture),
                    credential_loader=lambda: SandboxCredentials('fixture-id', 'fixture-secret'))
                self.assertEqual(result['payment_truth'], truth)

    def test_actual_app_receipt_view_is_user_facing_and_does_not_initialize_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'must-not-be-created.sqlite3'
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(['--db', str(db), 'sandbox-order'])
            self.assertEqual(result, 0)
            self.assertFalse(db.exists())
            self.assertIn('[PAYPAL SANDBOX READ-ONLY]', output.getvalue())
            self.assertIn(KNOWN_ORDER_ID, output.getvalue())
            self.assertIn('CREATED_NOT_APPROVED', output.getvalue())
            self.assertNotIn('CAPTURED', output.getvalue())

    def test_sandbox_human_review_uses_receipt_and_refuses_unsupported_paid_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'must-not-be-created.sqlite3'
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(['--db', str(db), 'sandbox-review'])
            rendered = output.getvalue()
            self.assertEqual(result, 0)
            self.assertFalse(db.exists())
            self.assertIn('[PAYPAL SANDBOX READ-ONLY]', rendered)
            self.assertIn('Source: synthetic_order_fixture', rendered)
            self.assertIn('This command displays facts only; it does not generate a draft.', rendered)
            self.assertIn('buyer approval and captured payment are not established', rendered)
            self.assertIn('python3 -B app.py --client stub sandbox-ai-review', rendered)
            self.assertIn('no approve, capture, refund, fulfillment, or send action', rendered)
            self.assertNotIn('suggestion digest', rendered.lower())

    def test_sandbox_human_review_refresh_uses_only_known_read_and_reports_live_time(self):
        live = {'order_id': KNOWN_ORDER_ID, 'order_status': 'CREATED', 'capture_status': None,
                'payment_truth': 'CREATED_NOT_APPROVED', 'currency': 'USD', 'amount': '10.00',
                'source': 'live_paypal_sandbox_read', 'read_only': True,
                'observed_at': '2026-10-29T23:50:00Z'}
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'must-not-be-created.sqlite3'
            output = io.StringIO()
            with patch('sandbox_reconciliation.refresh_known_order', return_value=live) as refresh:
                with redirect_stdout(output):
                    result = main(['--db', str(db), 'sandbox-review', '--refresh'])
            self.assertEqual(result, 0)
            self.assertFalse(db.exists())
            refresh.assert_called_once_with()
            self.assertIn('Source: live_paypal_sandbox_read', output.getvalue())
            self.assertIn('Live read observed at: 2026-10-29T23:50:00Z', output.getvalue())
            self.assertIn('CREATED_NOT_APPROVED', output.getvalue())
            self.assertIn('This command displays facts only; it does not generate a draft.', output.getvalue())
            self.assertIn('For a separate offline draft using synthetic example data', output.getvalue())


if __name__ == '__main__':
    unittest.main()
