"""Safe Granite failure diagnostics through the real local HTTP browser route.

The Ollama/provider call is mocked. No PayPal request, model inference, prompt,
raw model output, credential, or response body is emitted by this test.
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import urllib.error
import urllib.request
import unittest
from unittest.mock import patch

import local_model
import runtime_guard
import sandbox_ai_review
import sandbox_review_ui
from local_model import GraniteClient, MODEL, MODEL_DIGEST
from sandbox_review_ui import ReviewSession, make_handler


PRIVATE_SENTINEL = 'do-not-display-private-provider-text'
RECEIPT = {'order_id':'REDACTEDORDERID','order_status':'CREATED',
    'capture_status':None,'payment_truth':'CREATED_NOT_APPROVED','currency':'USD',
    'amount':'10.00','observed_at':'2026-10-30T12:00:00Z',
    'source':'saved_paypal_sandbox_receipt'}


class BrowserGraniteDiagnosticsTests(unittest.TestCase):
    cases = {
        'tag_lookup': ('tag_lookup','MODEL_TAG_LOOKUP_FAILED',False,0,None),
        'digest_check': ('digest_check','MODEL_DIGEST_MISMATCH',False,0,None),
        'admission': ('admission','ADMISSION_CHECK_FAILED',False,0,None),
        'resource_guard': ('resource_guard','RESOURCE_ADMISSION_DENIED',False,0,None),
        'swap_nonzero': ('resource_guard','SWAP_NONZERO',False,0,None),
        'http': ('model_request','MODEL_HTTP_ERROR',True,1,503),
        'response_envelope': ('response_parse','MODEL_RESPONSE_INVALID',True,1,None),
        'selection_contract': ('selection_contract','SELECTION_CONTRACT_REJECTED',True,1,None),
    }

    def _round_trip(self, mode):
        client = GraniteClient(lock_path=self._lock_path)
        if mode == 'tag_lookup':
            client.preflight = lambda: (_ for _ in ()).throw(runtime_guard.PilotStop(
                PRIVATE_SENTINEL, stage='tag_lookup', failure_code='MODEL_TAG_LOOKUP_FAILED'))
        elif mode == 'digest_check':
            client.preflight = lambda: {'model_digests': {MODEL:'unreviewed'}}
        elif mode == 'admission':
            client.preflight = lambda: (_ for _ in ()).throw(runtime_guard.PilotStop(
                PRIVATE_SENTINEL, stage='admission', failure_code='ADMISSION_CHECK_FAILED'))
        elif mode == 'resource_guard':
            client.preflight = lambda: (_ for _ in ()).throw(runtime_guard.PilotStop(
                PRIVATE_SENTINEL, stage='resource_guard', failure_code='RESOURCE_ADMISSION_DENIED'))
        elif mode == 'swap_nonzero':
            client.preflight = lambda: (_ for _ in ()).throw(runtime_guard.PilotStop(
                PRIVATE_SENTINEL, stage='resource_guard', failure_code='SWAP_NONZERO'))
        else:
            client.preflight = lambda: {'model_digests': {MODEL:MODEL_DIGEST}}

        if mode == 'http':
            generated = runtime_guard.PilotStop(PRIVATE_SENTINEL, stage='model_request',
                failure_code='MODEL_HTTP_ERROR', http_status=503)
            generate_patch = patch('local_model.guard.generate', side_effect=generated)
        elif mode == 'response_envelope':
            generate_patch = patch('local_model.guard.generate', return_value={'code':None})
        elif mode == 'selection_contract':
            generate_patch = patch('local_model.guard.generate', return_value={
                'code':PRIVATE_SENTINEL, 'metrics':{}, 'wall_seconds':0.1,
                'resource_samples':[]})
        else:
            generate_patch = patch('local_model.guard.generate')

        with generate_patch, patch('local_model.guard.get_json', return_value={'models':[]}), \
                patch.object(sandbox_review_ui, 'read_recorded_evidence',
                return_value=RECEIPT), patch.object(sandbox_ai_review, 'KNOWN_ORDER_ID',
                'REDACTEDORDERID'):
            session = ReviewSession(client)
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(session))
            server.daemon_threads = True
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            url = f'http://127.0.0.1:{server.server_port}'
            try:
                with urllib.request.urlopen(url + '/', timeout=3) as page_response:
                    self.assertEqual(page_response.status, 200)
                    page = page_response.read()
                req = urllib.request.Request(url + '/api/generate',
                    data=json.dumps({'question':'Has my payment gone through?'}).encode(),
                    headers={'Content-Type':'application/json','Origin':url}, method='POST')
                try:
                    with urllib.request.urlopen(req, timeout=3) as response:
                        status, body = response.status, response.read()
                except urllib.error.HTTPError as error:
                    status, body = error.code, error.read()
                    error.close()
                with urllib.request.urlopen(url + '/api/state', timeout=3) as response:
                    api_state = json.loads(response.read())
                return status, body, session, client, page, api_state
            finally:
                server.shutdown(); server.server_close(); worker.join(timeout=2)

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._lock_path = __import__('pathlib').Path(self._tmp.name) / 'granite.lock'

    def tearDown(self):
        self._tmp.cleanup()

    def test_bounded_stage_diagnostics_survive_browser_api_state_and_renderer(self):
        for mode, expected in self.cases.items():
            with self.subTest(mode=mode):
                status, body, session, client, page, api_state = self._round_trip(mode)
                self.assertEqual(status, 409)
                self.assertEqual(json.loads(body), {'error':'Draft generation stopped; no external action occurred.'})
                failure = session.state()['generation_failure']
                stage, code, attempted, count, http_status = expected
                self.assertEqual((failure['stage'],failure['code'],failure['request_attempted'],
                                  failure['attempt_count']), (stage,code,attempted,count))
                if http_status is not None:
                    self.assertEqual(failure['http_status'],http_status)
                else:
                    self.assertNotIn('http_status',failure)
                self.assertFalse(client.execution_verified)
                self.assertIn(b'granite-diagnostic', page)
                self.assertIn(b'No retry was made', page)
                self.assertEqual(api_state['generation_failure'], failure)
                self.assertNotIn(PRIVATE_SENTINEL, body.decode())
                self.assertNotIn(PRIVATE_SENTINEL, json.dumps(failure))
                self.assertIsNone(session.state()['draft'])
                self.assertEqual(session.state()['external_action'],'none')

    def test_review_ui_has_unique_ids_and_single_generation_diagnostic_binding(self):
        page = Path(__file__).with_name('sandbox-review-ui.html').read_text(encoding='utf-8')
        ids = re.findall(r'\bid="([^"]+)"', page)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(page.count('const diagnostic = state.generation_failure;'), 1)


if __name__ == '__main__':
    unittest.main()
