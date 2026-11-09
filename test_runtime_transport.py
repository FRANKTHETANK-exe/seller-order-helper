"""Inspect the exact localhost request using an in-memory HTTP substitute."""
import io
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from facts import OrderFacts
from suggestions import _prompt
import runtime_guard as guard


class RuntimeTransportTests(unittest.TestCase):
    def _queue_fixture(self, root, proof_boot=100):
        runtime = root/'SecondBrain'/'runtime'
        development = runtime/'platform-phase2'/'development-runtime'
        p2 = development/'P2'
        for directory in (runtime/'queue', runtime/'running', p2/'queue', p2/'running',
                          p2/'identities'):
            directory.mkdir(parents=True, exist_ok=True)
        task_path = p2/'running'/'P2-old.json'
        identity_path = p2/'identities'/'P2-old.json'
        task_path.write_text(json.dumps({'task_id':'P2-old','status':'queued'}))
        identity_path.write_text(json.dumps({
            'task_id':'P2-old','state':'running','claimed_by':'test-host:42:abcdef12',
            'execution_id':'a'*32,'queue_name':'P2-old.json',
            'claimed_at':'1970-01-01T00:01:00+00:00',
            'lease_expires_at':'1970-01-01T00:02:00+00:00'}))
        os.utime(task_path, (50,50)); os.utime(identity_path, (50,50))
        proof_path = root/'guard-evidence.json'
        proof_path.write_text(json.dumps({'boot_epoch':proof_boot,'entries':[{
            'relative_path':'platform-phase2/development-runtime/P2/running/P2-old.json',
            'task_id':'P2-old','task_sha256':hashlib.sha256(task_path.read_bytes()).hexdigest(),
            'identity_sha256':hashlib.sha256(identity_path.read_bytes()).hexdigest()}]}))
        return proof_path, task_path

    def test_prior_boot_proof_revalidates_unchanged_expired_task(self):
        for proof_boot in (100,201):
            with self.subTest(proof_boot=proof_boot), \
                 tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
                root=Path(tmp)
                proof, _ = self._queue_fixture(root, proof_boot=proof_boot)
                with patch('runtime_guard.Path.home',return_value=root), \
                     patch('runtime_guard.INSPECTED_STALE_PROOF',proof), \
                     patch('liveness.current_os_metadata',return_value=(200,set(),'test-host')):
                    counts=guard.queue_counts()
                self.assertEqual(counts['queue'],0)
                self.assertEqual(counts['running'],0)
                self.assertEqual(counts['inspected_stale_preserved'],1)

    def test_prior_boot_proof_does_not_hide_changed_or_future_records(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            root=Path(tmp)
            proof, task = self._queue_fixture(root, proof_boot=100)
            task.write_text(json.dumps({'task_id':'P2-old','status':'running','changed':True}))
            with patch('runtime_guard.Path.home',return_value=root), \
                 patch('runtime_guard.INSPECTED_STALE_PROOF',proof), \
                 patch('liveness.current_os_metadata',return_value=(200,set(),'test-host')):
                counts=guard.queue_counts()
            self.assertEqual(counts['running'],1)
            data=json.loads(proof.read_text()); data['boot_epoch']=300
            proof.write_text(json.dumps(data))
            with patch('runtime_guard.Path.home',return_value=root), \
                 patch('runtime_guard.INSPECTED_STALE_PROOF',proof), \
                 patch('liveness.current_os_metadata',return_value=(200,set(),'test-host')):
                with self.assertRaisesRegex(guard.PilotStop,'future boot'):
                    guard.queue_counts()

    def test_monitor_records_standalone_scope_without_claiming_queue_counts(self):
        local={'memory_pressure_free_percent':55,'swap_used_mib':0}
        portable=guard.resource_observation(local, 1.234, standalone_demo=True)
        self.assertEqual(portable['controller_queue_visibility'], 'not_checked_standalone_mode')
        self.assertNotIn('queue_counts', portable)
        controller=guard.resource_observation(
            {**local,'queue_counts':{'queue':0,'running':0}},
            1.234, standalone_demo=False)
        self.assertEqual(controller['queue_counts'], {'queue':0,'running':0})
        self.assertEqual(controller['controller_queue_visibility'], 'checked')

    def test_schema_payload_is_loopback_only_bounded_and_request_scoped(self):
        prompt=_prompt(OrderFacts('SIM-TRANSPORT-1','APPROVED'))
        schema=json.loads(prompt)['selection_schema']
        selection=json.dumps({'order_id':'SIM-TRANSPORT-1','category':'payment_pending',
                              'phrase_id':'item_first','style_id':'concise'})
        class Response(io.BytesIO):status=200
        class Connection:
            sock=None
            constructor=None;sent=None
            def __init__(self,*args,**kwargs):Connection.constructor=(args,kwargs)
            def request(self,method,path,body,headers):Connection.sent=(method,path,json.loads(body),headers)
            def getresponse(self):return Response((json.dumps({'response':selection,'done':True,'eval_count':50,'eval_duration':1000000000})+'\n').encode())
            def close(self):pass
        with patch('runtime_guard.http.client.HTTPConnection',Connection):
            result=guard.generate('granite4.2:3b',prompt,response_format=schema)
        self.assertEqual(Connection.constructor,(('127.0.0.1',11434),{'timeout':guard.LIMITS['request_seconds']}))
        method,path,payload,headers=Connection.sent
        self.assertEqual((method,path),('POST','/api/generate'))
        self.assertEqual(payload['format'],schema)
        self.assertEqual(payload['options']['num_ctx'],4096)
        self.assertEqual(payload['options']['num_predict'],256)
        self.assertEqual(payload['keep_alive'],0)
        self.assertFalse(payload['think'])
        self.assertEqual(headers,{'Content-Type':'application/json'})
        self.assertEqual(result['code'],selection)

    def test_health_routes_cannot_address_paypal_or_change_service(self):
        with patch('runtime_guard.http.client.HTTPConnection') as conn:
            for path in ('https://api-m.sandbox.paypal.com','/api/pull','/api/delete'):
                with self.assertRaises(guard.PilotStop):guard.get_json(path)
            conn.assert_not_called()

    def test_standalone_preflight_pins_local_digest_without_reading_private_queue_proof(self):
        from local_model import MODEL, MODEL_DIGEST
        responses={'/api/version':{'version':'0.40.13'},
                   '/api/tags':{'models':[{'name':MODEL,'digest':MODEL_DIGEST}]},
                   '/api/ps':{'models':[]}}
        resources={'memory_pressure_free_percent':55,'swap_used_mib':0,
                   'load_1m':1.2,'disk_free_bytes':80*guard.GIB}
        with patch('runtime_guard.sys.platform','darwin'), \
             patch('runtime_guard.get_json',side_effect=lambda p:responses[p]), \
             patch('runtime_guard.local_resources',return_value=resources), \
             patch('runtime_guard.queue_counts',side_effect=AssertionError('private queue inspected')):
            result=guard.inspect_standalone({MODEL:MODEL_DIGEST})
        self.assertEqual(result['admission'],'portable_local_checks_passed')
        self.assertEqual(result['controller_queue_visibility'],'not_checked_standalone_mode')
        self.assertEqual(result['model_digests'][MODEL],MODEL_DIGEST)

    def test_standalone_preflight_holds_on_digest_mismatch_and_loaded_models(self):
        from local_model import MODEL, MODEL_DIGEST
        resources={'memory_pressure_free_percent':55,'swap_used_mib':0,
                   'load_1m':1.2,'disk_free_bytes':80*guard.GIB}
        base={'/api/version':{'version':'0.40.13'},
              '/api/tags':{'models':[{'name':MODEL,'digest':'changed'}]},
              '/api/ps':{'models':[]}}
        with patch('runtime_guard.sys.platform','darwin'), \
             patch('runtime_guard.get_json',side_effect=lambda p:base[p]), \
             patch('runtime_guard.local_resources',return_value=resources):
            with self.assertRaisesRegex(guard.PilotStop,'digest changed'):
                guard.inspect_standalone({MODEL:MODEL_DIGEST})
        base['/api/tags']['models'][0]['digest']=MODEL_DIGEST
        base['/api/ps']={'models':[{'name':'other','size_vram':1}]}
        with patch('runtime_guard.sys.platform','darwin'), \
             patch('runtime_guard.get_json',side_effect=lambda p:base[p]), \
             patch('runtime_guard.local_resources',return_value=resources):
            with self.assertRaises(guard.PilotStop):guard.inspect_standalone({MODEL:MODEL_DIGEST})

    def test_standalone_preflight_preserves_specific_swap_reason_without_leaking_output(self):
        from local_model import MODEL, MODEL_DIGEST
        responses={'/api/version':{'version':'0.40.13'},
                   '/api/tags':{'models':[{'name':MODEL,'digest':MODEL_DIGEST}]},
                   '/api/ps':{'models':[]}}
        resources={'memory_pressure_free_percent':63,'swap_used_mib':880.31,
                   'load_1m':1.8,'disk_free_bytes':80*guard.GIB}
        with patch('runtime_guard.sys.platform','darwin'), \
             patch('runtime_guard.get_json',side_effect=lambda p:responses[p]), \
             patch('runtime_guard.local_resources',return_value=resources):
            with self.assertRaises(guard.PilotStop) as caught:
                guard.inspect_standalone({MODEL:MODEL_DIGEST})
        self.assertEqual((caught.exception.stage,caught.exception.failure_code),
                         ('resource_guard','SWAP_NONZERO'))
        self.assertNotIn('880.31',str(caught.exception))

    def test_controller_preflight_preserves_specific_swap_reason(self):
        from local_model import MODEL, MODEL_DIGEST
        responses={'/api/version':{'version':'0.40.13'},
                   '/api/tags':{'models':[{'name':MODEL,'digest':MODEL_DIGEST}]},
                   '/api/ps':{'models':[]}}
        resources={'memory_pressure_free_percent':63,'swap_used_mib':880.31,
                   'load_1m':1.8,'disk_free_bytes':80*guard.GIB,
                   'queue_counts':{'queue':0,'running':0}}
        with patch('runtime_guard.get_json',side_effect=lambda p:responses[p]), \
             patch('runtime_guard.resources',return_value=resources):
            with self.assertRaises(guard.PilotStop) as caught:
                guard.inspect()
        self.assertEqual((caught.exception.stage,caught.exception.failure_code),
                         ('resource_guard','SWAP_NONZERO'))


if __name__=='__main__':unittest.main()
