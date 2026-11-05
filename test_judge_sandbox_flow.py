"""Offline tests for the explicit one-shot judge Sandbox flow; no sockets."""
from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import reviewer_sandbox_order
import sandbox_ai_review
from facts import PRODUCT_ID, PRODUCT_NAME
from local_model import StubClient
from reviewer_sandbox_order import read_order_pin
from sandbox_adapter import (AdapterError, HttpResponse, SandboxCredentials,
                             create_payload)
from judge_sandbox_flow import main, run_once


ORDER_ID = 'TESTORDER12345678'
TOKEN = 'FIXTURE_TOKEN_MUST_NOT_PERSIST'
SECRET = 'FIXTURE_SECRET_MUST_NOT_PERSIST'
DEBUG_ID = 'debug-0123456789'


def order_body(order_id=ORDER_ID, status='CREATED', *, captures=None):
    unit = {'reference_id': PRODUCT_ID,
            'amount': {'currency_code': 'USD', 'value': '10.00'},
            'items': [{'name': PRODUCT_NAME, 'sku': PRODUCT_ID, 'quantity': '1',
                       'unit_amount': {'currency_code': 'USD', 'value': '10.00'}}]}
    if captures is not None:
        unit['payments'] = {'captures': captures}
    return {'id': order_id, 'status': status, 'purchase_units': [unit]}


class FakeTransport:
    def __init__(self, responses, on_request=None):
        self.responses = list(responses)
        self.requests = []
        self.on_request = on_request

    def request(self, request):
        self.requests.append(request)
        if self.on_request:
            self.on_request(request)
        if not self.responses:
            raise AssertionError('unexpected additional transport request')
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.journal = self.root / 'attempt.json'
        self.pin = self.root / 'order-pin.json'
        self.creds = SandboxCredentials('fixture-client-id', SECRET)

    def tearDown(self):
        self.tmp.cleanup()

    def token_response(self, *, status=200, body=None, debug=None):
        if body is None:
            body = json.dumps({'access_token': TOKEN, 'token_type': 'Bearer',
                               'expires_in': 3600, 'scope': ''}).encode()
        return HttpResponse(status, body, debug)

    def successful_transport(self):
        created = order_body()
        return FakeTransport([
            self.token_response(debug=DEBUG_ID),
            HttpResponse(201, json.dumps(created).encode(), DEBUG_ID),
            HttpResponse(200, json.dumps(order_body()).encode(), DEBUG_ID),
        ])

    def run_flow(self, transport, *, confirm=True, client=None, pin_path=None,
                 legacy_order_id=None):
        original = reviewer_sandbox_order.read_order_pin
        selected_pin = pin_path or self.pin
        isolated_legacy_pin = self.root / 'isolated-legacy-pin.json'
        isolated_default_judge_pin = self.root / 'isolated-default-judge-pin.json'
        if legacy_order_id is not None:
            reviewer_sandbox_order.write_order_pin(isolated_legacy_pin, legacy_order_id)

        def read_temp_pin(path=None):
            # Fail closed if a test ever reaches a real home-directory pin.
            candidate = Path(path) if path is not None else isolated_legacy_pin
            if self.root not in candidate.parents:
                raise AssertionError('test attempted to read outside its temporary directory')
            return original(candidate)

        with patch.object(reviewer_sandbox_order, 'read_order_pin', read_temp_pin), \
             patch.object(sandbox_ai_review, 'read_order_pin', read_temp_pin), \
             patch.object(reviewer_sandbox_order, 'DEFAULT_ORDER_PIN', isolated_legacy_pin), \
             patch.object(sandbox_ai_review, 'DEFAULT_ORDER_PIN', isolated_legacy_pin), \
             patch.object(reviewer_sandbox_order, 'DEFAULT_JUDGE_ORDER_PIN',
                          isolated_default_judge_pin):
            return run_once(transport=transport, credentials=self.creds,
                granite_client=client or StubClient(), question='Has my payment gone through?',
                confirm_create=lambda: confirm, journal_path=self.journal,
                pin_path=selected_pin, request_id_factory=lambda: 'fixture-request-001',
                now=lambda: '2026-10-31T00:00:00.000Z')

    def journal_data(self):
        return json.loads(self.journal.read_text())

    def test_success_is_oauth_create_immediate_get_and_one_draft(self):
        def assert_attempt_is_durable(request):
            saved = json.loads(self.journal.read_text())
            self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
            self.assertEqual(saved['calls'][-1]['outcome'], 'pending')
            self.assertTrue(saved['idempotencyKey'])
            self.assertEqual(saved['calls'][-1]['requestAt'], '2026-10-31T00:00:00.000Z')
        transport = self.successful_transport()
        transport.on_request = assert_attempt_is_durable
        result = self.run_flow(transport)
        self.assertEqual(result['state'], 'draft_pending_human_review')
        self.assertEqual(result['order_status'], 'CREATED')
        self.assertEqual(result['payment_truth'], 'CREATED_NOT_APPROVED')
        self.assertEqual(result['draft'].human_review_required, True)
        self.assertEqual([r.method for r in transport.requests], ['POST', 'POST', 'GET'])
        self.assertEqual([r.url.rsplit('/', 1)[-1] for r in transport.requests[1:]],
                         ['orders', ORDER_ID])
        self.assertEqual(transport.requests[1].headers['PayPal-Request-Id'],
                         'fixture-request-001')
        payload = json.loads(transport.requests[1].body)
        self.assertNotIn('payee', payload['purchase_units'][0])
        self.assertEqual(payload['purchase_units'][0]['amount'],
                         {'currency_code': 'USD', 'value': '10.00',
                          'breakdown': {'item_total': {'currency_code': 'USD', 'value': '10.00'}}})
        saved = self.journal_data()
        self.assertEqual(saved['state'], 'draft_pending_human_review')
        self.assertEqual([c['stage'] for c in saved['calls']],
                         ['oauth', 'order_create', 'order_get'])
        self.assertTrue(all(c['requestAt'] and c['responseAt'] for c in saved['calls']))
        self.assertTrue(all(c['httpStatus'] in (200, 201) for c in saved['calls']))
        self.assertTrue(all(c['debugId'] == DEBUG_ID for c in saved['calls']))
        serialized = self.journal.read_text()
        self.assertNotIn(TOKEN, serialized)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn('Has my payment', serialized)
        self.assertNotIn('draftDigest', saved)
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.pin.stat().st_mode & 0o777, 0o600)

    def test_judge_pin_is_used_for_live_fact_validation_not_legacy_pin(self):
        transport = self.successful_transport()
        result = self.run_flow(transport, legacy_order_id='OTHERORDER12345678')
        self.assertEqual(result['state'], 'draft_pending_human_review')
        self.assertEqual([request.method for request in transport.requests], ['POST', 'POST', 'GET'])
        self.assertEqual(result['draft'].payment_truth, 'CREATED_NOT_APPROVED')
        self.assertEqual(self.journal_data()['state'], 'draft_pending_human_review')

    def test_custom_pin_context_is_shared_with_local_model_parser(self):
        custom_pin = self.root / 'explicit-custom-pin.json'
        transport = self.successful_transport()
        result = self.run_flow(transport, pin_path=custom_pin)
        self.assertEqual(result['state'], 'draft_pending_human_review')
        self.assertEqual(result['draft'].payment_truth, 'CREATED_NOT_APPROVED')
        self.assertEqual([request.method for request in transport.requests], ['POST', 'POST', 'GET'])
        self.assertTrue(custom_pin.is_file())
        self.assertEqual(custom_pin.stat().st_mode & 0o777, 0o600)

    def test_cancel_before_create_makes_no_file_or_network_request(self):
        transport = self.successful_transport()
        result = self.run_flow(transport, confirm=False)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(transport.requests, [])
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.pin.exists())

    def test_cli_without_explicit_run_flag_is_inert(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([]), 0)
        self.assertEqual(json.loads(output.getvalue()),
                         {'state': 'not_run', 'reason': 'explicit_run_flag_required'})

    def test_fresh_judge_flow_uses_pin_separate_from_existing_reviewer_order(self):
        from judge_sandbox_flow import DEFAULT_JUDGE_ORDER_PIN
        from reviewer_sandbox_order import DEFAULT_ORDER_PIN
        self.assertEqual(DEFAULT_JUDGE_ORDER_PIN.name, 'paypal-judge-sandbox-order.json')
        self.assertNotEqual(DEFAULT_JUDGE_ORDER_PIN, DEFAULT_ORDER_PIN)
        self.assertEqual(inspect.signature(run_once).parameters['pin_path'].default,
                         DEFAULT_JUDGE_ORDER_PIN)

    def test_oauth_failure_retains_status_and_stops_before_order_write(self):
        transport = FakeTransport([HttpResponse(401, b'{"name":"AUTHENTICATION_FAILURE"}', DEBUG_ID)])
        with self.assertRaises(AdapterError):
            self.run_flow(transport)
        saved = self.journal_data()
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(saved['calls'][0]['stage'], 'oauth')
        self.assertEqual(saved['calls'][0]['httpStatus'], 401)
        self.assertEqual(saved['calls'][0]['errorName'], 'AUTHENTICATION_FAILURE')
        self.assertEqual(saved['calls'][0]['debugId'], DEBUG_ID)
        self.assertEqual(saved['state'], 'stopped')

    def test_order_create_rejection_keeps_error_name_and_never_gets(self):
        transport = FakeTransport([self.token_response(),
            HttpResponse(403, b'{"name":"NOT_AUTHORIZED","message":"private"}', DEBUG_ID)])
        with self.assertRaises(AdapterError):
            self.run_flow(transport)
        saved = self.journal_data()
        self.assertEqual([c['stage'] for c in saved['calls']], ['oauth', 'order_create'])
        self.assertEqual(saved['calls'][1]['httpStatus'], 403)
        self.assertEqual(saved['calls'][1]['errorName'], 'NOT_AUTHORIZED')
        self.assertNotIn('private', self.journal.read_text())
        self.assertEqual(len(transport.requests), 2)

    def test_get_404_retains_only_safe_metadata_and_does_not_generate(self):
        transport = FakeTransport([self.token_response(), HttpResponse(201,
            json.dumps(order_body()).encode(), DEBUG_ID),
            HttpResponse(404, b'{"name":"RESOURCE_NOT_FOUND","message":"private-id detail"}', DEBUG_ID)])
        with self.assertRaises(AdapterError) as caught:
            self.run_flow(transport)
        saved = self.journal_data()
        self.assertEqual(caught.exception.http_status, 404)
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(saved['calls'][2]['errorName'], 'RESOURCE_NOT_FOUND')
        self.assertEqual(saved['calls'][2]['debugId'], DEBUG_ID)
        self.assertEqual(saved['model']['status'], 'not_started')
        self.assertNotIn('private-id detail', self.journal.read_text())
        self.assertFalse(self.pin.exists())

    def test_uncertain_post_is_never_replayed(self):
        class Uncertain:
            def __init__(self): self.requests = []
            def request(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    return self.token_response()
                raise TimeoutError('secret transport detail')
            token_response = lambda self: HttpResponse(200,
                json.dumps({'access_token': TOKEN, 'token_type': 'Bearer', 'expires_in': 3600}).encode())
        transport = Uncertain()
        with self.assertRaises(AdapterError):
            self.run_flow(transport)
        before = len(transport.requests)
        with self.assertRaisesRegex(AdapterError, 'already exists'):
            self.run_flow(transport)
        self.assertEqual(len(transport.requests), before)
        saved = self.journal_data()
        self.assertEqual(saved['calls'][1]['stage'], 'order_create')
        self.assertEqual(saved['calls'][1]['outcome'], 'transport_error')
        self.assertEqual(saved['calls'][1]['failureCode'], 'SANDBOX_TIMEOUT')
        self.assertNotIn('secret transport detail', self.journal.read_text())

    def test_cli_uses_explicit_new_pin_and_journal_end_to_end(self):
        transport = self.successful_transport()
        client = StubClient()
        client.preflight = lambda: {'fixture': True}
        with patch('app._make_client', return_value=client), \
             patch('judge_sandbox_flow.UrllibSandboxTransport', return_value=transport), \
             patch('judge_sandbox_flow.load_owner_sandbox_credentials', return_value=self.creds), \
             patch('builtins.input', side_effect=['Has my payment gone through?',
                 'CREATE ONE USD 10 SANDBOX ORDER']), redirect_stdout(StringIO()):
            code = main(['--run', '--reviewer-order-pin', str(self.pin),
                         '--journal', str(self.journal)])
        self.assertEqual(code, 0)
        self.assertEqual(read_order_pin(self.pin).order_id, ORDER_ID)
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(self.journal_data()['state'], 'draft_pending_human_review')

    def test_explicit_resource_proof_is_scoped_and_restored_on_preflight_failure(self):
        import runtime_guard
        prior = runtime_guard.INSPECTED_STALE_PROOF
        proof = self.root / 'reviewed-proof.json'
        client = StubClient()
        def fail_preflight():
            self.assertEqual(runtime_guard.INSPECTED_STALE_PROOF, proof)
            raise runtime_guard.PilotStop('fixture admission held', stage='resource_guard', failure_code='RESOURCE_ADMISSION_DENIED')
        client.preflight = fail_preflight
        with patch('app._make_client', return_value=client), \
             patch('liveness.inspect_proof', return_value=({}, 1)), \
             patch('builtins.input', side_effect=['Has my payment gone through?', 'CREATE ONE USD 10 SANDBOX ORDER']), \
             patch('judge_sandbox_flow.UrllibSandboxTransport') as transport, \
             redirect_stdout(StringIO()):
            code = main(['--run', '--reviewer-order-pin', str(self.pin),
                '--journal', str(self.journal), '--resource-proof', str(proof)])
        self.assertEqual(code, 2)
        self.assertEqual(runtime_guard.INSPECTED_STALE_PROOF, prior)
        transport.assert_not_called()
        self.assertFalse(self.journal.exists())

    def test_successful_terminal_journal_binds_explicit_pin_and_rejects_tamper(self):
        self.run_flow(self.successful_transport(), legacy_order_id='OTHERORDER12345678')
        verified = reviewer_sandbox_order.validate_judge_order_binding(
            pin_path=self.pin, journal_path=self.journal)
        self.assertEqual(verified.order_id, ORDER_ID)
        value = self.journal_data()
        self.assertEqual(value['model']['completedAt'], '2026-10-31T00:00:00.000Z')
        self.assertEqual(value['model']['expectedModel'], 'granite4.2:3b')
        self.assertFalse(value['model']['executionVerified'])  # fixture is not real inference
        value['configurationId'] = '00000000-0000-4000-8000-000000000000'
        self.journal.write_text(json.dumps(value))
        with self.assertRaises(AdapterError):
            reviewer_sandbox_order.validate_judge_order_binding(
                pin_path=self.pin, journal_path=self.journal)

    def test_standalone_cli_uses_attested_portable_client_without_controller_proof(self):
        client = StubClient()
        transport = self.successful_transport()
        with patch('app._make_client', return_value=client) as factory, \
             patch('app._confirm_standalone_demo', return_value=True) as admission, \
             patch('judge_sandbox_flow.UrllibSandboxTransport', return_value=transport), \
             patch('judge_sandbox_flow.load_owner_sandbox_credentials', return_value=self.creds), \
             patch('liveness.inspect_proof') as proof, \
             patch('builtins.input', side_effect=['Has my payment gone through?', 'CREATE ONE USD 10 SANDBOX ORDER']), \
             redirect_stdout(StringIO()):
            code = main(['--run', '--standalone', '--reviewer-order-pin', str(self.pin),
                         '--journal', str(self.journal)])
        self.assertEqual(code, 0)
        factory.assert_called_once_with('granite', standalone_demo=True, stable_swap_policy=None)
        admission.assert_called_once_with(client)
        proof.assert_not_called()
        self.assertEqual(len(transport.requests), 3)

    def test_portable_admission_rejection_makes_no_external_call(self):
        with patch('app._make_client', return_value=StubClient()), \
             patch('app._confirm_standalone_demo', return_value=False), \
             patch('judge_sandbox_flow.UrllibSandboxTransport') as transport, \
             patch('builtins.input', side_effect=['Has my payment gone through?', 'CREATE ONE USD 10 SANDBOX ORDER']), \
             redirect_stdout(StringIO()):
            code = main(['--run', '--standalone', '--reviewer-order-pin', str(self.pin),
                         '--journal', str(self.journal)])
        self.assertEqual(code, 2)
        transport.assert_not_called()
        self.assertFalse(self.journal.exists())

    def test_atomic_pin_publication_does_not_replace_previous_pin(self):
        from judge_sandbox_flow import atomic_new_order_pin
        reviewer_sandbox_order.write_order_pin(self.pin, 'OTHERORDER12345678')
        previous = self.pin.read_bytes()
        with self.assertRaises(AdapterError):
            atomic_new_order_pin(self.pin, ORDER_ID)
        self.assertEqual(self.pin.read_bytes(), previous)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_cli_requires_explicit_paths_before_any_client_or_request(self):
        with patch('app._make_client') as factory, redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit):
                main(['--run'])
        factory.assert_not_called()

    def test_existing_pin_blocks_before_confirmation_or_network(self):
        self.pin.write_text('{}')
        transport = self.successful_transport()
        with self.assertRaisesRegex(AdapterError, 'pin is present'):
            self.run_flow(transport)
        self.assertEqual(transport.requests, [])
        self.assertFalse(self.journal.exists())

    def test_error_name_parser_rejects_private_or_unbounded_values(self):
        from judge_sandbox_flow import _safe_error_name
        self.assertEqual(_safe_error_name(b'{"name":"RESOURCE_NOT_FOUND","message":"secret"}'),
                         'RESOURCE_NOT_FOUND')
        self.assertEqual(_safe_error_name(b'{"error":"invalid_client","error_description":"secret"}'),
                         'invalid_client')
        self.assertIsNone(_safe_error_name(b'{"name":"not safe","message":"secret"}'))
        self.assertIsNone(_safe_error_name(b'{"error":"bad value","error_description":"secret"}'))
        self.assertIsNone(_safe_error_name(b'{"message":"secret"}'))

    def test_http_error_body_read_failure_keeps_status_and_debug_id(self):
        import urllib.error
        class BrokenRead:
            def read(self, limit):
                raise TimeoutError('private body read detail')
            def close(self):
                pass
        error = urllib.error.HTTPError(
            'https://api-m.sandbox.paypal.com/v1/oauth2/token', 401,
            'Unauthorized', {'PayPal-Debug-Id': DEBUG_ID}, BrokenRead())
        class Opener:
            def open(self, request, timeout): raise error
        from sandbox_adapter import HttpRequest, OAUTH_TOKEN_URL, UrllibSandboxTransport
        transport = UrllibSandboxTransport()
        transport._opener = Opener()
        response = transport.request(HttpRequest('POST', OAUTH_TOKEN_URL, {}, b'grant_type=client_credentials'))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.debug_id, DEBUG_ID)
        self.assertEqual(response.body, b'')
        self.assertNotIn('private body read detail', repr(response))

    def test_invalid_create_response_is_recorded_as_uncertain_and_never_replayed(self):
        transport = FakeTransport([self.token_response(),
            HttpResponse(201, b'{"id":"bad-id","status":"CREATED"}', DEBUG_ID)])
        with self.assertRaises(AdapterError) as caught:
            self.run_flow(transport)
        self.assertEqual(caught.exception.failure_code, 'ORDER_CREATE_RESPONSE_INVALID')
        saved = self.journal_data()
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(saved['calls'][1]['stage'], 'order_create')
        self.assertEqual(saved['calls'][1]['httpStatus'], 201)
        self.assertEqual(saved['calls'][1]['debugId'], DEBUG_ID)
        self.assertIsNone(saved['orderId'])
        self.assertNotIn('order_get', [call['stage'] for call in saved['calls']])
        with self.assertRaisesRegex(AdapterError, 'already exists'):
            self.run_flow(transport)
        self.assertEqual(len(transport.requests), 2)

    def test_non_http_response_stops_before_followup_calls(self):
        transport = FakeTransport([self.token_response(), object()])
        with self.assertRaises(AdapterError) as caught:
            self.run_flow(transport)
        self.assertEqual(caught.exception.failure_code, 'MALFORMED_HTTP_RESPONSE')
        saved = self.journal_data()
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(saved['calls'][1]['outcome'], 'malformed_response')
        self.assertEqual(saved['calls'][1]['failureCode'], 'MALFORMED_HTTP_RESPONSE')
        self.assertNotIn('order_get', [call['stage'] for call in saved['calls']])
        self.assertFalse(self.pin.exists())

    def test_approved_readback_never_creates_a_draft_or_pin(self):
        transport = FakeTransport([self.token_response(),
            HttpResponse(201, json.dumps(order_body()).encode(), DEBUG_ID),
            HttpResponse(200, json.dumps(order_body(status='APPROVED')).encode(), DEBUG_ID)])
        result = self.run_flow(transport)
        self.assertEqual(result['state'], 'readback_rejected')
        self.assertIsNone(result['draft'])
        self.assertEqual(len(transport.requests), 3)
        self.assertFalse(self.pin.exists())
        saved = self.journal_data()
        self.assertEqual(saved['model']['status'], 'not_started')
        self.assertEqual(saved['failure']['code'], 'ORDER_NOT_REVIEWABLE')

    def test_transport_preserves_only_validated_paypal_debug_header(self):
        class Response:
            status = 200
            headers = {'PayPal-Debug-Id': DEBUG_ID, 'Authorization': SECRET}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return b'{}'
        class Opener:
            def open(self, request, timeout): return Response()
        from sandbox_adapter import HttpRequest, OAUTH_TOKEN_URL, UrllibSandboxTransport
        transport = UrllibSandboxTransport()
        transport._opener = Opener()
        response = transport.request(HttpRequest('POST', OAUTH_TOKEN_URL, {}, b'grant_type=client_credentials'))
        self.assertEqual(response.debug_id, DEBUG_ID)
        self.assertNotIn(SECRET, repr(response))


if __name__ == '__main__':
    unittest.main()
