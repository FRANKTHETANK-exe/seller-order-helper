"""Runtime stream failures are classified without retaining response bodies."""
import unittest
from unittest.mock import patch

import runtime_guard
from runtime_guard import StableSwapBaseline, PilotStop

SENTINEL='raw-model-response-must-not-be-retained'


class FakeResponse:
    def __init__(self,status,line=b''):
        self.status=status; self.line=line; self.read_count=0
    def readline(self,_limit):
        self.read_count+=1
        return self.line if self.read_count==1 else b''


class FakeConnection:
    def __init__(self,response): self.response=response; self.sock=None; self.closed=False
    def request(self,*_args,**_kwargs): pass
    def getresponse(self): return self.response
    def close(self): self.closed=True


class TimerStub:
    def __init__(self,*_args,**_kwargs): pass
    def start(self): pass
    def cancel(self): pass


class ThreadStub:
    def __init__(self,*_args,**_kwargs): pass
    def start(self): pass
    def join(self,timeout=None): pass


class RuntimeGuardDiagnosticTests(unittest.TestCase):
    def run_with_response(self,response):
        conn=FakeConnection(response)
        with patch('runtime_guard.http.client.HTTPConnection',return_value=conn), \
             patch('runtime_guard.threading.Timer',TimerStub), \
             patch('runtime_guard.threading.Thread',ThreadStub):
            with self.assertRaises(runtime_guard.PilotStop) as caught:
                runtime_guard.generate('granite4.2:3b','{}',response_format={'type':'object'})
        self.assertTrue(conn.closed)
        self.assertNotIn(SENTINEL,str(caught.exception))
        return caught.exception

    def test_http_failure_keeps_only_bounded_status(self):
        error=self.run_with_response(FakeResponse(503,SENTINEL.encode()))
        self.assertEqual((error.stage,error.failure_code,error.http_status),
                         ('model_request','MODEL_HTTP_ERROR',503))

    def test_malformed_stream_json_is_response_parse_failure_without_body(self):
        error=self.run_with_response(FakeResponse(200,(SENTINEL+'\n').encode()))
        self.assertEqual((error.stage,error.failure_code,error.http_status),
                         ('response_parse','MODEL_RESPONSE_INVALID',None))

    def test_stable_swap_requires_three_fresh_identical_samples_and_all_gates(self):
        samples=[{'swap_used_mib':880.31,'memory_pressure_free_percent':55,
                  'load_1m':1.0,'disk_free_bytes':30*runtime_guard.GIB,
                  'queue_counts':{'queue':0,'running':0}} for _ in range(3)]
        sleeps=[]
        policy=StableSwapBaseline(sample_fn=lambda:samples.pop(0),
            loaded_fn=lambda:{'models':[]},sleep_fn=sleeps.append)
        self.assertEqual(policy.establish(),880.31)
        self.assertEqual(sleeps,[10,10])
        policy.close()
        with self.assertRaisesRegex(PilotStop,'not active'):
            policy.check({'swap_used_mib':880.31},{'models':[]})
        with self.assertRaisesRegex(PilotStop,'nonzero'):
            runtime_guard.guard({'swap_used_mib':880.31,'memory_pressure_free_percent':55,
                'load_1m':1.0,'disk_free_bytes':30*runtime_guard.GIB,
                'queue_counts':{'queue':0,'running':0}},{'models':[]})

    def test_stable_swap_rejects_drift_missing_metric_and_other_gate_failures(self):
        valid={'swap_used_mib':5,'memory_pressure_free_percent':55,
               'load_1m':1.0,'disk_free_bytes':30*runtime_guard.GIB,
               'queue_counts':{'queue':0,'running':0}}
        policy=StableSwapBaseline(sample_fn=lambda:dict(valid),
            loaded_fn=lambda:{'models':[]},sleep_fn=lambda _:None)
        policy.establish()
        with self.assertRaises(PilotStop) as drift:
            policy.check({**valid,'swap_used_mib':5.01},{'models':[]})
        self.assertEqual(drift.exception.failure_code,'SWAP_BASELINE_DRIFT')
        with self.assertRaises(PilotStop) as missing:
            policy.check({**valid,'swap_used_mib':None},{'models':[]})
        self.assertEqual(missing.exception.failure_code,'SWAP_BASELINE_INVALID')
        with self.assertRaises(PilotStop):
            policy.check({**valid,'memory_pressure_free_percent':39},{'models':[]})
        with self.assertRaises(PilotStop):
            StableSwapBaseline(sample_fn=lambda:{**valid,'load_1m':7},
                loaded_fn=lambda:{'models':[]},sleep_fn=lambda _:None).establish()
        with self.assertRaises(PilotStop):
            StableSwapBaseline(sample_fn=lambda:{**valid,'queue_counts':{'queue':1,'running':0}},
                loaded_fn=lambda:{'models':[]},sleep_fn=lambda _:None).establish()

    def test_established_baseline_is_rechecked_without_restarting_sampling_window(self):
        valid={'swap_used_mib':7,'memory_pressure_free_percent':55,
               'load_1m':1.0,'disk_free_bytes':30*runtime_guard.GIB,
               'queue_counts':{'queue':0,'running':0}}
        samples=[]
        policy=StableSwapBaseline(sample_fn=lambda:(samples.append(1) or dict(valid)),
            loaded_fn=lambda:{'models':[]},sleep_fn=lambda _:None)
        policy.establish()
        baseline_calls=len(samples)
        with patch('runtime_guard.get_json',side_effect=[{'version':'x'},
                {'models':[{'name':'granite4.2:3b','digest':'a'*64}]}, {'models':[]}]), \
             patch('runtime_guard.resources',return_value=valid):
            runtime_guard.inspect(stable_swap_policy=policy)
        self.assertEqual(len(samples),baseline_calls)
        self.assertEqual(policy.baseline_mib,7)

    def test_stable_swap_baseline_expires_after_one_minute(self):
        valid={'swap_used_mib':7,'memory_pressure_free_percent':55,
               'load_1m':1.0,'disk_free_bytes':30*runtime_guard.GIB,
               'queue_counts':{'queue':0,'running':0}}
        policy=StableSwapBaseline(sample_fn=lambda:dict(valid),
            loaded_fn=lambda:{'models':[]},sleep_fn=lambda _:None)
        policy.establish()
        with patch('runtime_guard.time.monotonic',return_value=policy.established_at+61):
            with self.assertRaises(PilotStop) as caught:
                policy.check(valid,{'models':[]})
        self.assertEqual(caught.exception.failure_code,'SWAP_BASELINE_INVALID')

    def test_stable_swap_monitor_drift_cancels_request_and_closes_policy(self):
        conn=FakeConnection(FakeResponse(200,b'{"response":"ok","done":true}\n'))
        sample={'swap_used_mib':10,'memory_pressure_free_percent':55,
                'load_1m':1.0,'disk_free_bytes':30*runtime_guard.GIB}
        policy=StableSwapBaseline(sample_fn=lambda:{**sample,'queue_counts':{'queue':0,'running':0}},
            loaded_fn=lambda:{'models':[]},sleep_fn=lambda _:None)
        policy.establish()
        class OneMonitorThenDone:
            def __init__(self,target,**_kwargs): self.target=target
            def start(self): self.target()
            def join(self,timeout=None): pass
        class ImmediateTimer(TimerStub): pass
        class ImmediateEvent:
            def __init__(self): self.calls=0; self.set_value=False
            def wait(self,_seconds):
                self.calls+=1
                return self.set_value or self.calls>1
            def is_set(self): return self.set_value
            def set(self): self.set_value=True
        with patch('runtime_guard.http.client.HTTPConnection',return_value=conn), \
             patch('runtime_guard.threading.Timer',ImmediateTimer), \
             patch('runtime_guard.threading.Thread',OneMonitorThenDone), \
             patch('runtime_guard.threading.Event',ImmediateEvent), \
             patch('runtime_guard.local_resources',return_value={**sample,'swap_used_mib':10.25}), \
             patch('runtime_guard.get_json',return_value={'models':[{'name':'granite4.2:3b','size_vram':1}]}):
            with self.assertRaises(PilotStop) as caught:
                runtime_guard.generate('granite4.2:3b','{}',response_format={'type':'object'},
                                       standalone_demo=True,stable_swap_policy=policy)
        self.assertEqual((caught.exception.stage,caught.exception.failure_code),
                         ('resource_guard','SWAP_BASELINE_DRIFT'))
        self.assertTrue(conn.closed)
        self.assertTrue(policy.closed)


if __name__=='__main__': unittest.main()
