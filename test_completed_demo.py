"""Synthetic transport/model integration; never native provider evidence."""
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from io import StringIO
from pathlib import Path
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

import judge_sandbox_flow as flow
from local_model import GraniteClient, StubClient
from private_file_support import supports_private_files
from sandbox_adapter import HttpResponse, SandboxCredentials
from sandbox_review_ui import CompletedReviewSession, ReviewUIError, make_handler
import test_judge_sandbox_flow as fixtures


@unittest.skipUnless(supports_private_files(), 'Native POSIX private files required')
class CompletedDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.root.chmod(0o700)
        self.pin, self.journal = self.root/'pin.json', self.root/'attempt.json'
        self.now = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
        self.model_calls = 0
        self.client = GraniteClient(standalone_demo=True)
        self.transport = fixtures.FakeTransport([
            HttpResponse(200, json.dumps({'access_token':'synthetic-token','token_type':'Bearer','expires_in':3600,'scope':''}).encode()),
            HttpResponse(201, json.dumps(fixtures.order_body()).encode()),
            HttpResponse(200, json.dumps(fixtures.order_body()).encode())])
        def simulated_model(client, prompt):
            self.model_calls += 1
            output = StubClient()(prompt)
            client.execution_verified = True
            client.last_run.update(status='completed', inference_requests_attempted=1, cleanup_confirmed=True)
            return output
        with patch.object(GraniteClient, '__call__', simulated_model):
            self.result = flow.run_once(transport=self.transport,
                credentials=SandboxCredentials('synthetic-client','synthetic-secret'),
                granite_client=self.client, question='Has this $10 order been paid?',
                confirm_create=lambda:True, pin_path=self.pin, journal_path=self.journal,
                now=lambda:self.now.isoformat().replace('+00:00','Z'))

    def session(self, **kwargs):
        return CompletedReviewSession(kwargs.pop('result', self.result), kwargs.pop('client', self.client),
            pin_path=self.pin, journal_path=self.journal, clock=kwargs.pop('clock',lambda:self.now), **kwargs)

    def assert_no_repeat(self):
        self.assertEqual(len(self.transport.requests), 3)
        self.assertEqual(self.model_calls, 1)

    def test_exact_draft_moves_to_review_without_historical_fallback_or_new_call(self):
        with patch('sandbox_review_ui.read_recorded_evidence', side_effect=AssertionError('historical fallback')):
            session = self.session()
        before = session.state()
        self.assertIs(session.suggestion, self.result['draft'])
        self.assertEqual(before['review_status'], 'pending_human_review')
        self.assertEqual(before['completed_buyer_question'], 'Has this $10 order been paid?')
        self.assertTrue(before['completed_flow']); self.assertTrue(before['live_facts_fresh'])
        self.assertFalse(before['live_refresh_enabled']); self.assertFalse(before['new_question_enabled'])
        state = session.review(before['draft']['digest'])
        self.assertEqual(state['review_status'], 'reviewed_not_sent')
        self.assertEqual(state['external_action'], 'none')
        self.assertFalse(state['draft']['sent']); self.assert_no_repeat()

    def test_replay_routes_and_duplicate_review_are_refused(self):
        session = self.session()
        for action in (session.refresh, session.generate, lambda:session.new_question(0)):
            with self.assertRaises(ReviewUIError): action()
        with self.assertRaises(ReviewUIError): session.review('0'*64)
        self.assertEqual(session.state()['review_status'], 'pending_human_review')
        session.review(session.suggestion.review_digest)
        with self.assertRaises(ReviewUIError): session.review(session.suggestion.review_digest)
        self.assert_no_repeat()

    def test_stale_and_changed_question_reviews_refuse_without_replay(self):
        session = self.session(clock=lambda:self.now+timedelta(seconds=301))
        self.assertFalse(session.state()['live_facts_fresh'])
        with self.assertRaises(ReviewUIError): session.review(session.suggestion.review_digest)
        with self.assertRaises(ReviewUIError): self.session().review(self.result['draft'].review_digest, question_version=1)
        self.assert_no_repeat()

    def test_tampered_draft_and_fact_binding_are_rejected(self):
        for changed in (replace(self.result['draft'], review_text='It was paid.'),
                        replace(self.result['draft'], sent=True),
                        replace(self.result['draft'], source='saved_paypal_sandbox_receipt')):
            with self.subTest(draft=changed):
                with self.assertRaises(ValueError): self.session(result={**self.result,'draft':changed})
        facts = replace(self.result['facts'], observed_at='2026-11-08T12:00:01Z')
        with self.assertRaises(ValueError): self.session(result={**self.result,'facts':facts})
        self.assert_no_repeat()

    def test_unverified_or_different_client_cannot_claim_completed_execution(self):
        with self.assertRaises(ValueError): self.session(client=StubClient())
        self.client.execution_verified = False
        with self.assertRaises(ValueError): self.session()
        self.client.execution_verified = True; self.client.last_run['inference_requests_attempted'] = 2
        with self.assertRaises(ValueError): self.session()

    def test_private_journal_must_confirm_model_completion(self):
        journal = json.loads(self.journal.read_text())
        for field, value in (('executionVerified',False),('expectedDigest','0'*64),('status','failed')):
            changed = {**journal, 'model':{**journal['model'], field:value}}
            self.journal.write_text(json.dumps(changed))
            with self.assertRaises(ValueError): self.session()
        self.assert_no_repeat()

    def test_cli_review_ui_uses_successful_result_and_existing_client(self):
        with (patch('app._make_client',return_value=self.client), patch('app._confirm_standalone_demo',return_value=True),
             patch('judge_sandbox_flow.run_once',return_value=self.result) as run,
             patch('sandbox_review_ui.serve_completed_review',return_value={'review_status':'reviewed_not_sent'}) as serve,
             patch('builtins.input',side_effect=['Has this $10 order been paid?',flow.CONFIRMATION]), redirect_stdout(StringIO())):
            code = flow.main(['--run','--standalone','--review-ui','--reviewer-order-pin',str(self.root/'new-pin.json'),
                              '--journal',str(self.root/'new-attempt.json')])
        self.assertEqual(code,0); run.assert_called_once()
        self.assertIs(serve.call_args.args[0],self.result); self.assertIs(serve.call_args.args[1],self.client)
        self.assertEqual(serve.call_args.kwargs['port'],0); self.assert_no_repeat()

    def test_ui_bind_failure_preserves_completed_flow_without_retry(self):
        output = StringIO()
        from contextlib import redirect_stderr
        with (patch('app._make_client',return_value=self.client), patch('app._confirm_standalone_demo',return_value=True),
             patch('judge_sandbox_flow.run_once',return_value=self.result) as run,
             patch('sandbox_review_ui.serve_completed_review',side_effect=OSError('fixture bind failure')),
             patch('builtins.input',side_effect=['Has this $10 order been paid?',flow.CONFIRMATION]),
             redirect_stdout(output), redirect_stderr(output)):
            code = flow.main(['--run','--standalone','--review-ui','--reviewer-order-pin',str(self.root/'new-pin.json'),
                              '--journal',str(self.root/'new-attempt.json')])
        self.assertEqual(code,2); run.assert_called_once()
        self.assertIn('do not rerun',output.getvalue()); self.assertTrue(self.journal.exists()); self.assert_no_repeat()

    def test_http_review_boundary_reuses_result_and_preserves_origin_check(self):
        session = self.session()
        server = ThreadingHTTPServer(('127.0.0.1',0),make_handler(session))
        server.daemon_threads=True
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            port=server.server_port
            def request(method,path,body=None,origin=None):
                connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                headers={'Content-Type':'application/json'}
                if origin: headers['Origin']=origin
                connection.request(method,path,json.dumps(body) if body is not None else None,headers)
                response=connection.getresponse(); value=json.loads(response.read()); connection.close()
                return response.status,value
            self.assertEqual(request('GET','/api/state')[1]['review_status'],'pending_human_review')
            digest=self.result['draft'].review_digest
            self.assertEqual(request('POST','/api/review',{'digest':digest},'https://elsewhere.invalid')[0],403)
            origin=f'http://127.0.0.1:{port}'
            for path,body in (('/api/generate',{'question':'Try again'}),('/api/refresh',{}),('/api/new-question',{'question_version':0})):
                self.assertEqual(request('POST',path,body,origin)[0],409)
            code,state=request('POST','/api/review',{'digest':digest},origin)
            self.assertEqual(code,200); self.assertEqual(state['review_status'],'reviewed_not_sent')
            self.assertEqual(request('GET','/api/state')[1]['draft']['digest'],digest)
            self.assert_no_repeat()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__': unittest.main()
