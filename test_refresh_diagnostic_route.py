"""Refresh diagnostics survive the actual loopback HTTP error response."""
import json
from http.server import ThreadingHTTPServer
import threading
import urllib.error
import urllib.request
import unittest

from sandbox_adapter import (HttpResponse, OAUTH_TOKEN_URL, SANDBOX_BASE_URL,
    SandboxAdapter, SandboxCredentials, SandboxOAuthClient)
from sandbox_reconciliation import KNOWN_ORDER_ID
from sandbox_review_ui import ReviewSession, make_handler


RECEIPT = {'order_id':KNOWN_ORDER_ID,'order_status':'CREATED',
    'capture_status':None,'payment_truth':'CREATED_NOT_APPROVED','currency':'USD',
    'amount':'10.00','observed_at':'2026-10-30T12:00:00Z',
    'source':'saved_paypal_sandbox_receipt'}
SECRET_MARKER = 'private-body-must-not-escape'


class RefreshDiagnosticRouteTests(unittest.TestCase):
    def _request_refresh(self, get_response):
        class FakeTransport:
            def __init__(self): self.requests=[]
            def request(self, request):
                self.requests.append(request)
                if request.url == OAUTH_TOKEN_URL:
                    return HttpResponse(200, b'{"access_token":"FAKE_TOKEN",'
                        b'"token_type":"Bearer","expires_in":3600}')
                return get_response

        transport=FakeTransport()
        def refresh():
            token=SandboxOAuthClient(transport,
                SandboxCredentials('fixture-id','fixture-secret'),enabled=True).fetch_token()
            return SandboxAdapter(transport,token).get_known_sandbox_order(KNOWN_ORDER_ID)

        session=ReviewSession(object(),refresh_known_order=refresh)
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(session))
        server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        request=urllib.request.Request(base+'/api/refresh',data=b'{}',
            headers={'Content-Type':'application/json','Origin':base},method='POST')
        try:
            try:
                with urllib.request.urlopen(request,timeout=3) as response:
                    status,body=response.status,response.read()
            except urllib.error.HTTPError as error:
                status,body=error.code,error.read(); error.close()
            return status,json.loads(body),session.state(),transport.requests
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_paypal_non_2xx_status_survives_adapter_session_and_loopback_route(self):
        status,payload,state,requests=self._request_refresh(
            HttpResponse(404,SECRET_MARKER.encode()))
        expected={'stage':'order_get_http','code':'PINNED_ORDER_GET_REJECTED',
                  'http_status':404}
        self.assertEqual(status,409)
        self.assertEqual(payload['diagnostic'],expected)
        self.assertEqual(state['live_refresh_failure'],expected)
        self.assertEqual([r.method for r in requests],['POST','GET'])
        safe=json.dumps({'payload':payload,'state':state})
        self.assertNotIn(SECRET_MARKER,safe)
        self.assertNotIn('FAKE_TOKEN',safe)
        self.assertEqual(state['source'],'synthetic_order_fixture')

    def test_http_200_schema_validation_survives_without_inventing_status(self):
        status,payload,state,requests=self._request_refresh(HttpResponse(
            200,json.dumps({'id':KNOWN_ORDER_ID,'status':'CREATED',
                'purchase_units':[]}).encode()))
        expected={'stage':'order_validation','code':'PURCHASE_UNIT_SHAPE_INVALID'}
        self.assertEqual(status,409)
        self.assertEqual(payload['diagnostic'],expected)
        self.assertEqual(state['live_refresh_failure'],expected)
        self.assertNotIn('http_status',payload['diagnostic'])
        self.assertEqual([r.method for r in requests],['POST','GET'])
        self.assertEqual(state['source'],'synthetic_order_fixture')


if __name__=='__main__': unittest.main()
