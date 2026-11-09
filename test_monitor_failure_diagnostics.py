"""Monitor cancellation must retain its cause without exposing provider text."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from facts import OrderFacts
import local_model
from local_model import GraniteClient, MODEL, MODEL_DIGEST
import runtime_guard as guard
from suggestions import _prompt


class MonitorFailureTests(unittest.TestCase):
    def test_monitor_socket_close_preserves_swap_reason(self):
        closed = threading.Event()
        class Socket:
            def shutdown(self, how): closed.set()
        class Connection:
            sock = Socket()
            calls = 0
            def __init__(self, *args, **kwargs): pass
            def request(self, *args, **kwargs): Connection.calls += 1
            def getresponse(self):
                if not closed.wait(5): raise AssertionError('Monitor did not cancel')
                raise http.client.RemoteDisconnected('private provider text')
            def close(self): closed.set()
        resources = {'memory_pressure_free_percent':55, 'swap_used_mib':0.01,
                     'memory_available_bytes':5*guard.GIB, 'load_1m':1,
                     'disk_free_bytes':40*guard.GIB}
        with patch.object(guard.sys, 'platform', 'darwin'), \
             patch.object(guard.http.client, 'HTTPConnection', Connection), \
             patch.object(guard, 'local_resources', return_value=resources), \
             patch.object(guard, 'get_json', return_value={'models':[]}):
            with self.assertRaises(guard.PilotStop) as caught:
                guard.generate(MODEL, _prompt(OrderFacts('SIM-MONITOR','CREATED')),
                               standalone_demo=True)
        self.assertEqual(Connection.calls, 1)
        self.assertEqual((caught.exception.stage, caught.exception.failure_code),
                         ('resource_guard','SWAP_NONZERO'))
        evidence = caught.exception.evidence
        self.assertEqual(evidence['transport_error_type'], 'RemoteDisconnected')
        self.assertEqual(evidence['last_observation']['swap_used_mib'],0.01)
        self.assertEqual(evidence['last_observation']['memory_available_bytes'],5*guard.GIB)
        self.assertNotIn('private provider text', str(caught.exception))

    @unittest.skipIf(__import__('sys').platform == 'win32', 'Native POSIX client test')
    def test_client_retains_sanitized_monitor_evidence_and_stays_single_use(self):
        failure=guard.PilotStop('private text',stage='resource_guard',failure_code='SWAP_NONZERO')
        failure.evidence={'monitor_stage':'resource_guard','monitor_failure_code':'SWAP_NONZERO',
            'monitor_phase':'guard','transport_error_type':'RemoteDisconnected',
            'last_observation':{'elapsed':52,'pressure_free_percent':55,'swap_used_mib':0.01,
                'prompt':'private text','load_1m':float('nan'),'memory_total_bytes':2**10000},
            'resource_samples':[{'elapsed':2,'pressure_free_percent':80,'swap_used_mib':0}],
            'response_body':'private text','credential':'private text'}
        with tempfile.TemporaryDirectory() as directory:
            client=GraniteClient(lock_path=Path(directory)/'model.lock')
            client.preflight=lambda:{'model_digests':{MODEL:MODEL_DIGEST}}
            with patch.object(guard,'generate',side_effect=failure) as generated, \
                 patch.object(guard,'get_json',return_value={'models':[]}):
                prompt=_prompt(OrderFacts('SIM-DIAGNOSTICS','CREATED'))
                with self.assertRaises(local_model.LocalModelHeld): client(prompt)
                with self.assertRaises(local_model.LocalModelHeld): client(prompt)
                self.assertEqual(generated.call_count,1)
        safe=client.last_run['failure_evidence']
        self.assertEqual(safe['last_observation'],{'elapsed':52,'pressure_free_percent':55,'swap_used_mib':0.01})
        self.assertNotIn('private text',json.dumps(client.last_run,allow_nan=False))
        self.assertEqual(client.last_run['error_code'],'SWAP_NONZERO')
        self.assertTrue(client.last_run['cleanup_confirmed'])
        self.assertFalse(client.execution_verified)

    def test_evidence_rejects_unknown_values_and_bounds_samples(self):
        value={'monitor_phase':['secret'], 'monitor_stage':'secret',
               'transport_error_type':'secret',
               'resource_samples':[{'elapsed':i,'swap_used_mib':0,'unknown':'secret'} for i in range(100)]}
        safe=local_model._safe_failure_evidence(value)
        self.assertEqual(len(safe['resource_samples']),64)
        self.assertNotIn('secret',json.dumps(safe))


if __name__ == '__main__': unittest.main()
