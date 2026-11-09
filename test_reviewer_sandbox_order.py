"""Offline checks for a reviewer-owned Sandbox order pin and read flow."""
import json
import os
from functools import partial
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import threading
import urllib.error
import urllib.request
import unittest
from unittest.mock import patch

from reviewer_sandbox_order import (ReviewerOrderPin, read_order_pin,
                                    validate_order_id, write_order_pin,
                                    validate_judge_order_binding)
from sandbox_adapter import (AdapterError, HttpResponse, OAUTH_TOKEN_URL,
                             SANDBOX_BASE_URL, SandboxCredentials)
from sandbox_ai_review import facts_from_verified_live_read
from sandbox_review_ui import ReviewSession
from sandbox_reconciliation import refresh_reviewer_order
from sandbox_review_ui import make_handler
from local_model import StubClient


ORDER_ID = 'A1B2C3D4E5F6G7H8'
CONFIG_ID = '12345678-1234-4234-9234-123456789abc'


class FakeSandboxTransport:
    def __init__(self, *, order_id=ORDER_ID, status='CREATED', capture=None):
        self.requests = []
        self.order_id = order_id
        self.status = status
        self.capture = capture

    def request(self, request):
        self.requests.append(request)
        if request.url == OAUTH_TOKEN_URL:
            return HttpResponse(200, json.dumps({'access_token':'FAKE_TOKEN',
                'token_type':'Bearer','expires_in':3600}).encode())
        purchase = {'amount': {'currency_code':'USD','value':'10.00'}}
        if self.capture:
            purchase['payments'] = {'captures':[{'status':self.capture,
                'amount':{'currency_code':'USD','value':'10.00'}}]}
        value = {'id':self.order_id,'status':self.status,'purchase_units':[purchase]}
        return HttpResponse(200, json.dumps(value).encode())


class FakeLocalModel:
    kind = 'granite'
    model_name = 'granite-fixture'
    standalone_demo = True
    execution_verified = True

    def __init__(self):
        self.prompt = None
        self.last_run = {'inference_requests_attempted':1}

    def __call__(self, prompt):
        self.prompt = prompt
        request = json.loads(prompt)
        return json.dumps({'order_id':request['facts']['order_id'],
            'question_intent':'payment_status','phrase_id':'status_first','style_id':'concise'})


def make_private_dir(base: Path) -> Path:
    path = base / '.secondbrain-secrets'
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


@unittest.skipUnless(__import__("private_file_support").supports_private_files(), "POSIX owner-private filesystem behavior unavailable")
class ReviewerOrderPinTests(unittest.TestCase):
    @staticmethod
    def write_successful_journal(path, *, order_id, configuration_id):
        journal = {
            'schema': 'judge-sandbox-create-read-draft-v1',
            'environment': 'PayPal Sandbox only',
            'state': 'fresh_order_read',
            'createdAt': '2026-10-31T02:48:34.273Z',
            'updatedAt': '2026-10-31T02:48:35.938Z',
            'orderObservedAt': '2026-10-31T02:48:35Z',
            'idempotencyKey': 'fixture-request-001',
            'orderId': order_id,
            'configurationId': configuration_id,
            'createdOrderStatus': 'CREATED',
            'orderStatus': 'CREATED',
            'paymentTruth': 'CREATED_NOT_APPROVED',
            'captureStatus': None,
            'currency': 'USD',
            'amount': '10.00',
            'calls': [
                {'stage': 'oauth', 'method': 'POST', 'httpStatus': 200, 'outcome': 'response'},
                {'stage': 'order_create', 'method': 'POST', 'httpStatus': 201, 'outcome': 'response'},
                {'stage': 'order_get', 'method': 'GET', 'httpStatus': 200, 'outcome': 'response'},
            ],
            'model': {'status': 'not_started', 'requestCount': 0},
        }
        path.write_text(json.dumps(journal))
        path.chmod(0o600)

    def test_judge_binding_requires_canonical_pin_and_matching_successful_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            parent=make_private_dir(Path(temp))
            judge=parent/'paypal-judge-sandbox-order.json'
            legacy=parent/'paypal-sandbox-order.json'
            journal=parent/'paypal-judge-flow-once.json'
            judge_id='JUDGEORDER12345678'
            legacy_id='LEGACYORDER12345678'
            judge_pin=write_order_pin(judge,judge_id,configuration_id=CONFIG_ID)
            write_order_pin(legacy,legacy_id,
                configuration_id='87654321-1234-4234-9234-123456789abc')
            self.write_successful_journal(journal,order_id=judge_id,
                                          configuration_id=judge_pin.configuration_id)
            with patch('reviewer_sandbox_order.DEFAULT_JUDGE_ORDER_PIN',judge), \
                 patch('reviewer_sandbox_order.DEFAULT_JUDGE_JOURNAL',journal):
                verified=validate_judge_order_binding(pin_path=judge)
                self.assertEqual(verified,judge_pin)
                with self.assertRaises(AdapterError) as error:
                    validate_judge_order_binding(pin_path=legacy)
                self.assertEqual(error.exception.failure_code,'JUDGE_PIN_PATH_MISMATCH')
                self.write_successful_journal(journal,order_id=legacy_id,
                    configuration_id='87654321-1234-4234-9234-123456789abc')
                with self.assertRaises(AdapterError) as error:
                    validate_judge_order_binding(pin_path=judge)
                self.assertEqual(error.exception.failure_code,'JUDGE_PIN_JOURNAL_MISMATCH')
                self.write_successful_journal(journal,order_id=judge_id,
                    configuration_id=judge_pin.configuration_id)
                value=json.loads(journal.read_text())
                value['environment']='PayPal sandbox only'
                journal.write_text(json.dumps(value)); journal.chmod(0o600)
                with self.assertRaises(AdapterError) as error:
                    validate_judge_order_binding(pin_path=judge)
                self.assertEqual(error.exception.failure_code,'JUDGE_PIN_JOURNAL_MISMATCH')

    def test_canonical_cli_validates_judge_journal_then_reads_same_pin_through_ui_route(self):
        import app
        import sandbox_reconciliation
        with tempfile.TemporaryDirectory() as temp:
            parent=make_private_dir(Path(temp))
            judge=parent/'paypal-judge-sandbox-order.json'
            legacy=parent/'paypal-sandbox-order.json'
            journal=parent/'paypal-judge-flow-once.json'
            judge_id='JUDGEORDER12345678'
            legacy_id='LEGACYORDER12345678'
            judge_pin=write_order_pin(judge,judge_id,configuration_id=CONFIG_ID)
            write_order_pin(legacy,legacy_id,
                configuration_id='87654321-1234-4234-9234-123456789abc')
            self.write_successful_journal(journal,order_id=judge_id,
                                          configuration_id=judge_pin.configuration_id)
            transport=FakeSandboxTransport(order_id=judge_id)
            client=FakeLocalModel()
            real_reader=sandbox_reconciliation.refresh_reviewer_order
            selected_paths=[]
            def fake_reader(*,pin_path=None):
                selected_paths.append(Path(pin_path))
                return real_reader(transport=transport,pin_path=pin_path,
                    credential_loader=lambda:SandboxCredentials('fixture-client-id','fixture-client-secret'))
            def exercise_ui(selected_client, *, port, refresh_known_order, reviewer_pin_path):
                session=ReviewSession(selected_client,refresh_known_order=refresh_known_order,
                                      reviewer_pin_path=reviewer_pin_path)
                server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(session))
                server.daemon_threads=True
                worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
                base=f'http://127.0.0.1:{server.server_port}'
                def request(path,payload=None):
                    req=(urllib.request.Request(base+path) if payload is None else
                         urllib.request.Request(base+path,data=json.dumps(payload).encode(),method='POST',
                             headers={'Content-Type':'application/json','Origin':base}))
                    with urllib.request.urlopen(req,timeout=3) as response:
                        return response.status,json.loads(response.read())
                try:
                    status,refreshed=request('/api/refresh',{})
                    self.assertEqual(status,200)
                    self.assertEqual(refreshed['order_id'],judge_id)
                    self.assertEqual(refreshed['payment_truth'],'CREATED_NOT_APPROVED')
                    status,draft=request('/api/generate',{'question':'Has my payment gone through?'})
                    self.assertEqual(status,200)
                    self.assertEqual(draft['review_status'],'pending_human_review')
                    self.assertEqual(draft['external_action'],'none')
                    self.assertNotIn(legacy_id,client.prompt)
                    self.assertNotIn(CONFIG_ID,client.prompt)
                finally:
                    server.shutdown();server.server_close();worker.join(timeout=2)
            client_args=[]
            def make_client(name, **kwargs):
                client_args.append((name, kwargs))
                return client
            with patch('reviewer_sandbox_order.DEFAULT_JUDGE_ORDER_PIN',judge), \
                 patch('reviewer_sandbox_order.DEFAULT_JUDGE_JOURNAL',journal), \
                 patch('reviewer_sandbox_order.DEFAULT_ORDER_PIN',legacy), \
                 patch('sandbox_ai_review.DEFAULT_ORDER_PIN',legacy), \
                 patch.object(app,'_make_client',side_effect=make_client), \
                 patch.object(app,'_confirm_standalone_demo',return_value=True), \
                 patch.object(sandbox_reconciliation,'refresh_reviewer_order',side_effect=fake_reader), \
                 patch('sandbox_review_ui.serve_review_ui',side_effect=exercise_ui):
                result=app.main(['--client','granite','sandbox-review-ui',
                    '--refresh-reviewer-order','--reviewer-order-pin',str(judge),
                    '--judge-order-verification','--stable-swap-baseline','--port','0'])
            self.assertEqual(result,0)
            self.assertEqual(selected_paths,[judge])
            self.assertEqual([request.method for request in transport.requests],['POST','GET'])
            self.assertEqual(transport.requests[1].url,
                SANDBOX_BASE_URL+'/v2/checkout/orders/'+judge_id)
            self.assertNotIn(legacy_id,transport.requests[1].url)
            self.assertEqual(client_args[0][0],'granite')
            from runtime_guard import StableSwapBaseline
            self.assertIsInstance(client_args[0][1]['stable_swap_policy'],StableSwapBaseline)

    def test_judge_cli_rejects_missing_explicit_pin_before_ui_or_network(self):
        import app
        output=[]
        with patch('builtins.print',side_effect=lambda *args,**kwargs: output.append(' '.join(map(str,args)))), \
             patch.object(app,'_make_client',return_value=FakeLocalModel()), \
             patch('sandbox_review_ui.serve_review_ui') as serve:
            result=app.main(['--client','granite','sandbox-review-ui',
                '--refresh-reviewer-order','--judge-order-verification','--port','0'])
        self.assertEqual(result,2)
        self.assertFalse(serve.called)
        self.assertTrue(any('--judge-order-verification requires' in line for line in output))

    def test_stable_swap_baseline_flag_requires_judge_verification(self):
        import app
        output=[]
        with patch('builtins.print',side_effect=lambda *args,**kwargs: output.append(' '.join(map(str,args)))), \
             patch.object(app,'_make_client',side_effect=AssertionError('must not construct client')):
            result=app.main(['--client','granite','sandbox-review-ui',
                '--refresh-reviewer-order','--reviewer-order-pin','/tmp/fixture-pin.json',
                '--stable-swap-baseline','--port','0'])
        self.assertEqual(result,2)
        self.assertTrue(any('--stable-swap-baseline requires --judge-order-verification' in line
                            for line in output))

    def test_cli_wires_reviewer_refresh_for_granite_or_explicit_stub_mode(self):
        import app
        from sandbox_reconciliation import refresh_reviewer_order as expected_reader
        client=FakeLocalModel()
        with patch.object(app,'_make_client',return_value=client), \
             patch.object(app,'_confirm_standalone_demo',return_value=True), \
             patch('sandbox_review_ui.serve_review_ui') as serve:
            self.assertEqual(app.main(['--client','granite','--portable-standalone',
                'sandbox-review-ui','--refresh-reviewer-order','--port','0']),0)
            args,kwargs=serve.call_args
            self.assertIs(args[0],client)
            self.assertIs(kwargs['refresh_known_order'],expected_reader)
            self.assertEqual(kwargs['port'],0)
        with patch.object(app,'_make_client',return_value=client), \
             patch.object(app,'_confirm_standalone_demo',return_value=True), \
             patch('sandbox_review_ui.serve_review_ui') as serve:
            self.assertEqual(app.main(['--client','stub','sandbox-review-ui',
                '--refresh-reviewer-order','--port','0']),0)
            args,kwargs=serve.call_args
            self.assertIs(args[0],client)
            self.assertIs(kwargs['refresh_known_order'],expected_reader)
            self.assertEqual(kwargs['port'],0)

    def test_cli_binds_explicit_private_judge_pin_path_through_ui_refresh(self):
        import app
        from sandbox_reconciliation import refresh_reviewer_order as expected_reader
        client=FakeLocalModel()
        pin_path=Path('/tmp/private-review-pin-fixture.json')
        with patch.object(app,'_make_client',return_value=client), \
             patch.object(app,'_confirm_standalone_demo',return_value=True), \
             patch('sandbox_review_ui.serve_review_ui') as serve:
            self.assertEqual(app.main(['--client','stub','sandbox-review-ui',
                '--refresh-reviewer-order','--reviewer-order-pin',str(pin_path),'--port','0']),0)
            _,kwargs=serve.call_args
            self.assertEqual(kwargs['reviewer_pin_path'],pin_path)
            self.assertIsInstance(kwargs['refresh_known_order'],partial)
            self.assertIs(kwargs['refresh_known_order'].func,expected_reader)
            self.assertEqual(kwargs['refresh_known_order'].keywords['pin_path'],pin_path)

    def test_explicit_reviewer_pin_requires_reviewer_refresh_mode(self):
        import app
        output=[]
        with patch('builtins.print',side_effect=lambda *args,**kwargs: output.append(' '.join(map(str,args)))), \
             patch.object(app,'_make_client',return_value=FakeLocalModel()):
            self.assertEqual(app.main(['sandbox-review-ui','--reviewer-order-pin','/tmp/pin.json']),2)
        self.assertTrue(any('--reviewer-order-pin requires --refresh-reviewer-order' in line for line in output))

    def test_pin_setup_writes_owner_only_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = make_private_dir(Path(temp)) / 'paypal-sandbox-order.json'
            pin = write_order_pin(path, ORDER_ID, configuration_id=CONFIG_ID)
            self.assertEqual(pin, ReviewerOrderPin(CONFIG_ID, ORDER_ID))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(read_order_pin(path), pin)
            self.assertNotIn(ORDER_ID, repr(pin))
            with self.assertRaisesRegex(AdapterError, 'already exists'):
                write_order_pin(path, 'Z9Y8X7W6V5U4T3S2')
            self.assertEqual(read_order_pin(path), pin)

    def test_pin_rejects_redactions_malformed_ids_and_malformed_schema(self):
        for value in ('REDACTEDORDERID','ORDER_ID_REDACTED','lowercase123456', 'TOO-SHORT'):
            with self.subTest(value=value), self.assertRaises(AdapterError):
                validate_order_id(value)
        with tempfile.TemporaryDirectory() as temp:
            path = make_private_dir(Path(temp)) / 'paypal-sandbox-order.json'
            path.write_text(json.dumps({'schema':'reviewer-sandbox-order-pin-v1',
                'configurationId':CONFIG_ID,'orderId':'REDACTEDORDERID'}))
            path.chmod(0o600)
            with self.assertRaisesRegex(AdapterError, 'failed validation'):
                read_order_pin(path)

    def test_pin_rejects_broad_permissions_symlinks_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = make_private_dir(Path(temp)); path = parent/'pin.json'
            write_order_pin(path, ORDER_ID, configuration_id=CONFIG_ID)
            path.chmod(0o644)
            with self.assertRaisesRegex(AdapterError, 'permissions'):
                read_order_pin(path)
            path.chmod(0o600)
            link = parent/'linked.json'; link.symlink_to(path)
            with self.assertRaises(AdapterError):
                read_order_pin(link)
            value = json.loads(path.read_text()); value['unexpected']='x'
            path.write_text(json.dumps(value)); path.chmod(0o600)
            with self.assertRaisesRegex(AdapterError, 'schema'):
                read_order_pin(path)

    def test_missing_pin_fails_before_credentials_and_network(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'missing.json'
            transport = FakeSandboxTransport(); credential_calls=[]
            with self.assertRaises(AdapterError) as caught:
                refresh_reviewer_order(transport=transport, pin_path=path,
                    credential_loader=lambda: credential_calls.append(True))
            self.assertEqual(caught.exception.stage,'configuration')
            self.assertEqual(caught.exception.safe_call_counts,
                             {'oauth_attempts':0,'pinned_order_get_attempts':0})
            self.assertEqual(credential_calls, [])
            self.assertEqual(transport.requests, [])

    def test_one_review_refresh_binds_config_identity_and_only_reads_order(self):
        with tempfile.TemporaryDirectory() as temp:
            parent=make_private_dir(Path(temp)); path=parent/'paypal-sandbox-order.json'
            pin=write_order_pin(path, ORDER_ID, configuration_id=CONFIG_ID)
            transport=FakeSandboxTransport(); client=FakeLocalModel()
            credentials=SandboxCredentials('fixture-id','fixture-secret')
            live=refresh_reviewer_order(transport=transport,pin_path=path,
                                        credential_loader=lambda:credentials)
            self.assertEqual(live['source'],'reviewer_live_paypal_sandbox_read')
            self.assertEqual(live['configuration_id'],CONFIG_ID)
            self.assertEqual(live['safe_call_counts'],
                             {'oauth_attempts':1,'pinned_order_get_attempts':1})
            self.assertEqual([request.method for request in transport.requests],['POST','GET'])
            self.assertEqual(transport.requests[1].url,
                             SANDBOX_BASE_URL+'/v2/checkout/orders/'+ORDER_ID)
            self.assertNotIn('FAKE_TOKEN',repr(transport.requests))

            with patch('sandbox_ai_review.read_order_pin',return_value=pin):
                session=ReviewSession(client,refresh_known_order=lambda:live)
                refreshed=session.refresh()
                draft=session.generate('Has my payment gone through?')
                facts=facts_from_verified_live_read(live)
            self.assertEqual(refreshed['payment_truth'],'CREATED_NOT_APPROVED')
            self.assertEqual(refreshed['live_refresh_status'],'succeeded')
            self.assertEqual(draft['review_status'],'pending_human_review')
            self.assertEqual(draft['external_action'],'none')
            self.assertNotIn(CONFIG_ID,client.prompt)
            self.assertEqual(facts.configuration_id,CONFIG_ID)
            self.assertEqual(facts.source,'reviewer_live_paypal_sandbox_read')

    def test_review_session_uses_same_explicit_pin_for_live_facts_and_model_parser(self):
        with tempfile.TemporaryDirectory() as temp:
            parent=make_private_dir(Path(temp)); path=parent/'judge-pin.json'
            pin=write_order_pin(path,ORDER_ID,configuration_id=CONFIG_ID)
            live={'order_id':ORDER_ID,'order_status':'CREATED','payment_truth':'CREATED_NOT_APPROVED',
                  'capture_status':None,'currency':'USD','amount':'10.00',
                  'source':'reviewer_live_paypal_sandbox_read','read_only':True,
                  'configuration_id':CONFIG_ID,'safe_call_counts':{'oauth_attempts':1,'pinned_order_get_attempts':1},
                  'observed_at':'2026-10-31T03:00:00Z'}
            legacy=parent/'different-legacy-pin.json'
            write_order_pin(legacy,'Z9Y8X7W6V5U4T3S2',configuration_id='87654321-1234-4234-9234-123456789abc')
            client=StubClient()
            session=ReviewSession(client,refresh_known_order=lambda:live,
                reviewer_pin_path=path,clock=lambda:datetime(2026,10,31,3,1,tzinfo=timezone.utc))
            state=session.refresh()
            draft=session.generate('Has my payment gone through?')
            self.assertEqual(state['payment_truth'],'CREATED_NOT_APPROVED')
            self.assertEqual(draft['review_status'],'pending_human_review')
            self.assertEqual(draft['external_action'],'none')
            self.assertEqual(session.facts.configuration_id,CONFIG_ID)
            # StubClient parses the grounded request through local_model.request_facts.
            # Success proves its parser saw the same explicit pin as the live reader;
            # the deliberately conflicting default pin would reject this receipt.
            self.assertEqual(client.last_run['status'],'stub')

    def test_live_receipt_rejects_wrong_or_changed_configuration_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            parent=make_private_dir(Path(temp)); path=parent/'pin.json'
            pin=write_order_pin(path,ORDER_ID,configuration_id=CONFIG_ID)
            transport=FakeSandboxTransport()
            live=refresh_reviewer_order(transport=transport,pin_path=path,
                credential_loader=lambda:SandboxCredentials('id','secret'))
            with patch('sandbox_ai_review.read_order_pin',return_value=pin):
                self.assertEqual(facts_from_verified_live_read(live).configuration_id,CONFIG_ID)
                wrong={**live,'configuration_id':'87654321-1234-4234-9234-123456789abc'}
                with self.assertRaisesRegex(ValueError,'active reviewer order configuration'):
                    facts_from_verified_live_read(wrong)

    def test_non_created_or_captured_order_is_rejected_after_read_without_draft(self):
        for kwargs in ({'status':'APPROVED'}, {'capture':'COMPLETED'}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as temp:
                parent=make_private_dir(Path(temp)); path=parent/'pin.json'
                write_order_pin(path,ORDER_ID,configuration_id=CONFIG_ID)
                transport=FakeSandboxTransport(**kwargs)
                with self.assertRaises(AdapterError) as caught:
                    refresh_reviewer_order(transport=transport,pin_path=path,
                        credential_loader=lambda:SandboxCredentials('id','secret'))
                self.assertEqual((caught.exception.stage,caught.exception.failure_code),
                                 ('order_validation','ORDER_STATE_NOT_REVIEWABLE'))
                self.assertEqual(caught.exception.safe_call_counts,
                                 {'oauth_attempts':1,'pinned_order_get_attempts':1})
                self.assertEqual([r.method for r in transport.requests],['POST','GET'])

    def test_actual_loopback_ui_refreshes_then_generates_one_review_draft(self):
        with tempfile.TemporaryDirectory() as temp:
            parent=make_private_dir(Path(temp)); path=parent/'paypal-sandbox-order.json'
            pin=write_order_pin(path,ORDER_ID,configuration_id=CONFIG_ID)
            legacy_path=parent/'different-legacy-order.json'
            write_order_pin(legacy_path,'Z9Y8X7W6V5U4T3S2',
                configuration_id='87654321-1234-4234-9234-123456789abc')
            transport=FakeSandboxTransport(); client=FakeLocalModel()
            live_reader=lambda: refresh_reviewer_order(transport=transport,pin_path=path,
                credential_loader=lambda:SandboxCredentials('fixture-client-id','fixture-client-secret'))
            receipt={'order_id':'REDACTEDORDERID','order_status':'CREATED',
                'capture_status':None,'payment_truth':'CREATED_NOT_APPROVED',
                'currency':'USD','amount':'10.00','observed_at':'2026-10-30T12:00:00Z',
                'source':'saved_paypal_sandbox_receipt'}
            with patch('sandbox_review_ui.read_recorded_evidence',return_value=receipt), \
                 patch('reviewer_sandbox_order.DEFAULT_ORDER_PIN',legacy_path), \
                 patch('sandbox_ai_review.DEFAULT_ORDER_PIN',legacy_path):
                from sandbox_review_ui import ReviewSession
                session=ReviewSession(client,refresh_known_order=live_reader,
                    reviewer_pin_path=path,
                    clock=lambda:datetime.now(timezone.utc))
                server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(session))
                server.daemon_threads=True
                worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
                base=f'http://127.0.0.1:{server.server_port}'
                def request(path,payload=None):
                    if payload is None:
                        req=urllib.request.Request(base+path)
                    else:
                        req=urllib.request.Request(base+path,data=json.dumps(payload).encode(),
                            method='POST',headers={'Content-Type':'application/json','Origin':base})
                    try:
                        with urllib.request.urlopen(req,timeout=2) as response:
                            return response.status,response.read()
                    except urllib.error.HTTPError as error:
                        try: return error.code,error.read()
                        finally: error.close()
                try:
                    self.assertEqual(request('/')[0],200)
                    status,body=request('/api/refresh',{})
                    self.assertEqual(status,200)
                    refreshed=json.loads(body)
                    self.assertEqual(refreshed['live_refresh_status'],'succeeded')
                    self.assertEqual(refreshed['payment_truth'],'CREATED_NOT_APPROVED')
                    self.assertEqual(refreshed['live_refresh_call_counts'],
                        {'oauth_attempts':1,'pinned_order_get_attempts':1})
                    status,body=request('/api/generate',{'question':'Has my payment gone through?'})
                    self.assertEqual(status,200)
                    draft=json.loads(body)
                    self.assertEqual(draft['review_status'],'pending_human_review')
                    self.assertEqual(draft['external_action'],'none')
                    self.assertFalse(draft['draft']['sent'])
                    self.assertNotIn(CONFIG_ID,client.prompt)
                    serialized=body.decode()
                    self.assertNotIn('FAKE_TOKEN',serialized)
                    self.assertNotIn('fixture-client-secret',serialized)
                    self.assertEqual([r.method for r in transport.requests],['POST','GET'])
                    self.assertEqual(request('/api/refresh',{})[0],409)
                    self.assertEqual(len(transport.requests),2)
                finally:
                    server.shutdown();server.server_close();worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
