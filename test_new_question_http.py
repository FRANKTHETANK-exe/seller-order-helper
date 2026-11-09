"""Exercise reset/replay and origin protection through the actual local routes."""
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from local_model import StubClient
from sandbox_review_ui import ReviewSession,make_handler

class NewQuestionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.session=ReviewSession(StubClient())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.session))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
    def post(self,path,value,origin=None):
        request=Request(self.base+path,data=json.dumps(value).encode(),method='POST',headers={
            'Content-Type':'application/json','Origin':origin or self.base})
        try:
            with urlopen(request,timeout=2) as result:return result.status,json.load(result)
        except HTTPError as result:
            try:return result.code,json.load(result)
            finally:result.close()
    def test_new_question_does_not_generate_and_old_tab_cannot_consume_new_attempt(self):
        code,first=self.post('/api/generate',{'question':'Has my payment gone through?','question_version':0})
        self.assertEqual(code,200);self.assertTrue(first['draft']['seller_next_step'])
        code,second=self.post('/api/new-question',{'question_version':0})
        self.assertEqual(code,200);self.assertIsNone(second['draft']);self.assertEqual(second['question_version'],1)
        for payload in ({'question':'Old tab'},{'question':'Old tab','question_version':0}):
            code,_=self.post('/api/generate',payload);self.assertEqual(code,409)
            self.assertFalse(self.session.attempted)
        code,_=self.post('/api/new-question',{'question_version':0});self.assertEqual(code,409)
        code,draft=self.post('/api/generate',{'question':'Was I charged twice?','question_version':1})
        self.assertEqual(code,200);self.assertIn('duplicate_charge',draft['draft']['seller_attention'])
        self.assertEqual(draft['review_status'],'pending_human_review')
        code,_=self.post('/api/review',{'digest':draft['draft']['digest'],'question_version':0})
        self.assertEqual(code,409);self.assertIsNone(self.session.review_result)
    def test_reset_requires_same_origin_and_explicit_current_version(self):
        self.session.generate()
        code,_=self.post('/api/new-question',{'question_version':0},'https://example.invalid')
        self.assertEqual(code,403);self.assertIsNotNone(self.session.suggestion)
        for value in ({},{'question_version':True},{'question_version':0,'run':True}):
            code,_=self.post('/api/new-question',value);self.assertIn(code,(404,409))
            self.assertTrue(self.session.attempted);self.assertEqual(self.session.question_version,0)

if __name__=='__main__':unittest.main()
