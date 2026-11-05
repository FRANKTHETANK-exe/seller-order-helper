"""Exercise failure classification through the actual loopback HTTP review route.

All PayPal transport and model behavior is faked. The tests start the same
ThreadingHTTPServer/handler used by the app, call its JSON endpoints, and read
the browser page that renders the resulting safe state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import json
import threading
import urllib.error
import urllib.request
import unittest
from unittest.mock import patch

from local_model import StubClient
from sandbox_adapter import (AdapterError, HttpResponse, OAUTH_TOKEN_URL,
                             SANDBOX_BASE_URL, SandboxCredentials)
import sandbox_reconciliation
import sandbox_ai_review
import sandbox_review_ui
from sandbox_reconciliation import KNOWN_ORDER_ID, refresh_known_order
from sandbox_review_ui import ReviewSession, make_handler


TOKEN_SENTINEL = 'offline-secret-token-sentinel'
BODY_SENTINEL = 'offline-private-response-sentinel'
TEST_ORDER_ID = 'TESTORDER123'
TEST_RECEIPT = {'order_id': TEST_ORDER_ID, 'order_status':'CREATED', 'capture_status':None,
    'payment_truth':'CREATED_NOT_APPROVED', 'currency':'USD', 'amount':'10.00',
    'observed_at':'2026-10-30T12:00:00Z', 'source':'saved_paypal_sandbox_receipt'}


class FakeTransport:
    def __init__(self, mode='success'):
        self.mode = mode
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        if request.url == OAUTH_TOKEN_URL:
            if self.mode == 'oauth_http':
                return HttpResponse(401, BODY_SENTINEL.encode())
            return HttpResponse(200, json.dumps({'access_token': TOKEN_SENTINEL,
                'token_type': 'Bearer', 'expires_in': 3600}).encode())
        if self.mode == 'transport':
            raise RuntimeError(BODY_SENTINEL)
        if self.mode == 'http':
            return HttpResponse(503, BODY_SENTINEL.encode())
        if self.mode == 'json':
            return HttpResponse(200, b'{not-json')
        body = {'id': TEST_ORDER_ID, 'status': 'CREATED',
                'purchase_units': [{'amount': {'currency_code': 'USD',
                                               'value': '10.00' if self.mode != 'schema' else '9.99'}}],
                'buyer': {'email_address': BODY_SENTINEL}}
        return HttpResponse(200, json.dumps(body).encode())


class BrowserRouteDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.server = None
        self.thread = None
        self.pin_patches = [
            patch.object(sandbox_reconciliation, 'KNOWN_ORDER_ID', TEST_ORDER_ID),
            patch.object(sandbox_reconciliation, 'read_recorded_evidence', return_value=TEST_RECEIPT),
            patch.object(sandbox_review_ui, 'read_recorded_evidence', return_value=TEST_RECEIPT),
            patch.object(sandbox_ai_review, 'KNOWN_ORDER_ID', TEST_ORDER_ID),
        ]
        for p in self.pin_patches: p.start()

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        for p in reversed(self.pin_patches): p.stop()

    def start(self, session):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(session))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def call(self, path, payload=None):
        if payload is None:
            request = urllib.request.Request(self.base + path)
        else:
            body = json.dumps(payload).encode()
            request = urllib.request.Request(self.base + path, data=body, method='POST',
                headers={'Content-Type': 'application/json', 'Origin': self.base})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read()
            finally:
                error.close()

    @staticmethod
    def callback(mode):
        transport = FakeTransport(mode)
        def callback():
            from unittest.mock import patch
            with patch.object(sandbox_reconciliation, 'KNOWN_ORDER_ID', TEST_ORDER_ID), \
                 patch.object(sandbox_ai_review, 'KNOWN_ORDER_ID', TEST_ORDER_ID), \
                 patch.object(sandbox_reconciliation, 'read_recorded_evidence', return_value=TEST_RECEIPT):
                return refresh_known_order(transport=transport,
                    credential_loader=lambda: SandboxCredentials('offline-client-id', 'offline-client-secret'))
        return callback, transport

    def test_transport_http_json_and_schema_failures_survive_api_state_and_browser_renderer(self):
        expected = {
            'transport': ('order_get_transport', 'SANDBOX_NETWORK_ERROR', None, 1, 1),
            'http': ('order_get_http', 'PINNED_ORDER_GET_REJECTED', 503, 1, 1),
            'json': ('order_get_response', 'ORDER_RESPONSE_JSON_INVALID', None, 1, 1),
            'schema': ('order_validation', 'ORDER_AMOUNT_CURRENCY_MISMATCH', None, 1, 1),
        }
        for mode, (stage, code, status, oauth_count, get_count) in expected.items():
            with self.subTest(mode=mode):
                callback, transport = self.callback(mode)
                self.start(ReviewSession(StubClient(), refresh_known_order=callback))
                html_status, html = self.call('/')
                self.assertEqual(html_status, 200)
                self.assertIn(b'live_refresh_failure', html)
                self.assertIn(b'call_counts', html)
                self.assertIn(b'Refresh stopped (', html)
                post_status, body = self.call('/api/refresh', {})
                self.assertEqual(post_status, 409)
                payload = json.loads(body)
                self.assertEqual(payload['error'],
                    'Live refresh failed validation. Saved receipt remains historical; no AI draft was generated.')
                state_status, state_body = self.call('/api/state')
                self.assertEqual(state_status, 200)
                state = json.loads(state_body)
                failure = state['live_refresh_failure']
                self.assertEqual((failure['stage'], failure['code']), (stage, code))
                if status is not None:
                    self.assertEqual(failure['http_status'], status)
                else:
                    self.assertNotIn('http_status', failure)
                self.assertEqual(state['live_refresh_call_counts'],
                    {'oauth_attempts': oauth_count, 'pinned_order_get_attempts': get_count})
                self.assertEqual([r.method for r in transport.requests], ['POST', 'GET'])
                self.assertEqual(state['source'], 'saved_paypal_sandbox_receipt')
                self.assertIsNone(state['draft'])
                serialized = body.decode() + state_body.decode() + html.decode()
                self.assertNotIn(TOKEN_SENTINEL, serialized)
                self.assertNotIn(BODY_SENTINEL, serialized)
                self.assertEqual(self.call('/api/refresh', {})[0], 409)
                self.assertEqual(len(transport.requests), 2)  # no retry
                self.tearDown()
                self.setUp()

    def test_live_fact_reconciliation_failure_is_distinct_and_stops_generation(self):
        callback, transport = self.callback('success')
        def mismatched():
            facts = callback()
            facts['payment_truth'] = 'CAPTURED'
            return facts
        session = ReviewSession(StubClient(), refresh_known_order=mismatched)
        self.start(session)
        self.assertEqual(self.call('/api/refresh', {})[0], 409)
        state = json.loads(self.call('/api/state')[1])
        self.assertEqual(state['live_refresh_failure'], {
            'stage': 'live_fact_reconciliation',
            'code': 'LIVE_FACT_RECONCILIATION_FAILED',
            'call_counts': {'oauth_attempts': 1, 'pinned_order_get_attempts': 1}})
        self.assertEqual(state['live_refresh_call_counts'],
                         {'oauth_attempts': 1, 'pinned_order_get_attempts': 1})
        self.assertEqual(self.call('/api/generate', {'question': 'Has my payment gone through?'})[0], 409)
        self.assertEqual(len(transport.requests), 2)

    def test_success_refresh_and_question_generation_cross_the_same_http_route(self):
        callback, transport = self.callback('success')
        session = ReviewSession(StubClient(phrase_id='status_first', style_id='concise'),
            refresh_known_order=callback, clock=lambda: datetime.now(timezone.utc))
        self.start(session)
        self.assertEqual(self.call('/')[0], 200)
        status, refresh_body = self.call('/api/refresh', {})
        self.assertEqual(status, 200)
        refreshed = json.loads(refresh_body)
        self.assertEqual(refreshed['source'], 'live_paypal_sandbox_read')
        self.assertEqual(refreshed['payment_truth'], 'CREATED_NOT_APPROVED')
        self.assertTrue(refreshed['live_facts_fresh'])
        self.assertEqual(refreshed['live_refresh_call_counts'],
                         {'oauth_attempts': 1, 'pinned_order_get_attempts': 1})
        status, draft_body = self.call('/api/generate', {'question': 'Has my payment gone through?'})
        self.assertEqual(status, 200)
        draft = json.loads(draft_body)
        self.assertEqual(draft['draft']['question_intent'], 'payment_status')
        self.assertEqual(draft['review_status'], 'pending_human_review')
        self.assertFalse(draft['draft']['sent'])
        self.assertEqual(draft['external_action'], 'none')
        self.assertEqual([r.method for r in transport.requests], ['POST', 'GET'])
        serialized = refresh_body.decode() + draft_body.decode()
        self.assertNotIn(TOKEN_SENTINEL, serialized)
        self.assertNotIn(BODY_SENTINEL, serialized)

    def test_stub_refresh_then_generate_is_explicitly_template_only(self):
        callback, transport = self.callback('success')
        client = StubClient(phrase_id='status_first', style_id='concise')
        self.start(ReviewSession(client, refresh_known_order=callback,
            clock=lambda: datetime.now(timezone.utc)))
        page_status, page = self.call('/')
        self.assertEqual(page_status, 200)
        html = page.decode()
        self.assertIn('Offline template · no AI inference', html)
        self.assertIn('Its factual sentence comes from the validated Sandbox read', html)
        self.assertIn('Offline template mode can use a validated one-time read', html)

        status, refresh_body = self.call('/api/refresh', {})
        self.assertEqual(status, 200)
        refreshed = json.loads(refresh_body)
        self.assertEqual(refreshed['source'], 'live_paypal_sandbox_read')
        self.assertEqual(refreshed['payment_truth'], 'CREATED_NOT_APPROVED')
        self.assertEqual(refreshed['live_refresh_call_counts'],
            {'oauth_attempts': 1, 'pinned_order_get_attempts': 1})

        status, draft_body = self.call('/api/generate',
            {'question': 'Has my payment gone through?'})
        self.assertEqual(status, 200)
        draft = json.loads(draft_body)
        self.assertEqual(draft['client_kind'], 'stub')
        self.assertEqual(draft['draft']['question_intent'], 'payment_status')
        self.assertEqual(draft['review_status'], 'pending_human_review')
        self.assertFalse(draft['draft']['sent'])
        self.assertEqual(draft['external_action'], 'none')
        self.assertEqual(client.last_run['inference_requests_attempted'], 0)
        self.assertEqual([request.method for request in transport.requests], ['POST', 'GET'])
        self.assertEqual(self.call('/api/refresh', {})[0], 409)
        self.assertEqual(len(transport.requests), 2)
        serialized = page.decode() + refresh_body.decode() + draft_body.decode()
        self.assertNotIn(TOKEN_SENTINEL, serialized)
        self.assertNotIn(BODY_SENTINEL, serialized)

    def test_oauth_http_failure_stops_before_pinned_get_through_route(self):
        callback, transport = self.callback('oauth_http')
        self.start(ReviewSession(StubClient(), refresh_known_order=callback))
        self.assertEqual(self.call('/api/refresh', {})[0], 409)
        state = json.loads(self.call('/api/state')[1])
        self.assertEqual((state['live_refresh_failure']['stage'],
                          state['live_refresh_failure']['code']),
                         ('oauth_http', 'OAUTH_HTTP_REJECTED'))
        self.assertEqual(state['live_refresh_call_counts'],
                         {'oauth_attempts': 1, 'pinned_order_get_attempts': 0})
        self.assertEqual([r.method for r in transport.requests], ['POST'])
        self.assertEqual(self.call('/api/refresh', {})[0], 409)
        self.assertEqual(len(transport.requests), 1)


if __name__ == '__main__':
    unittest.main()
