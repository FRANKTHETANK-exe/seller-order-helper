"""Targeted v35 review assertions. All providers/resources are fixtures."""
import contextlib,io,json,unittest
from pathlib import Path
from unittest.mock import patch
import test_judge_sandbox_flow as fixture
from local_model import StubClient,GraniteClient,MODEL,MODEL_DIGEST
from runtime_guard import PilotStop,StableSwapBaseline
from sandbox_adapter import AdapterError
from judge_sandbox_flow import main

class ReviewVerificationTests(unittest.TestCase):
 def setUp(self):
  self.flow=fixture.FlowTests();self.flow.setUp();self.addCleanup(self.flow.tearDown)
 def args(self):
  return ['--run','--reviewer-order-pin',str(self.flow.pin),'--journal',str(self.flow.journal)]
 def test_question_and_confirmation_finish_before_resource_baseline_and_error_json_is_safe(self):
  order=[];client=StubClient();err=io.StringIO()
  def answer(prompt):
   order.append('question' if 'Synthetic' in prompt else 'confirmation')
   return 'Has my payment gone through?' if 'Synthetic' in prompt else 'CREATE ONE USD 10 SANDBOX ORDER'
  def preflight():
   order.append('preflight');raise PilotStop('PRIVATE_SENTINEL',stage='resource_guard',failure_code='RESOURCE_ADMISSION_DENIED')
  client.preflight=preflight
  with patch('app._make_client',return_value=client),patch('builtins.input',side_effect=answer) as inputs,patch('judge_sandbox_flow.UrllibSandboxTransport') as transport,contextlib.redirect_stderr(err),contextlib.redirect_stdout(io.StringIO()):
   self.assertEqual(main(self.args()),2)
  self.assertEqual(inputs.call_count,2);self.assertEqual(order,['question','confirmation','preflight'])
  self.assertEqual(json.loads(err.getvalue()),{'state':'stopped','stage':'resource_guard','code':'RESOURCE_ADMISSION_DENIED','httpStatus':None})
  transport.assert_not_called();self.assertNotIn('PRIVATE_SENTINEL',err.getvalue());self.assertFalse(self.flow.journal.exists())
 def test_supported_preflight_error_branches_remain_bounded_without_raw_messages(self):
  cases=[(AdapterError('PRIVATE_SENTINEL',stage='oauth',failure_code='OAUTH_HTTP_ERROR',http_status=401),'oauth','OAUTH_HTTP_ERROR',401),
   (PilotStop('PRIVATE_SENTINEL',stage='tag_lookup',failure_code='MODEL_TAG_UNAVAILABLE'),'tag_lookup','MODEL_TAG_UNAVAILABLE',None),
   (ValueError('PRIVATE_SENTINEL'),'flow','FLOW_STOPPED',None),(EOFError('PRIVATE_SENTINEL'),'flow','FLOW_STOPPED',None),(KeyboardInterrupt('PRIVATE_SENTINEL'),'flow','FLOW_STOPPED',None)]
  for error,stage,code,status in cases:
   with self.subTest(kind=type(error).__name__):
    client=StubClient();client.preflight=lambda:(_ for _ in ()).throw(error);err=io.StringIO()
    with patch('app._make_client',return_value=client),patch('builtins.input',side_effect=['Has my payment gone through?','CREATE ONE USD 10 SANDBOX ORDER']),patch('judge_sandbox_flow.UrllibSandboxTransport') as transport,contextlib.redirect_stderr(err),contextlib.redirect_stdout(io.StringIO()):
     self.assertEqual(main(self.args()),2)
    self.assertEqual(json.loads(err.getvalue()),{'state':'stopped','stage':stage,'code':code,'httpStatus':status});self.assertNotIn('PRIVATE_SENTINEL',err.getvalue());transport.assert_not_called()
 def test_standalone_admission_also_follows_both_prompts(self):
  order=[];transport=self.flow.successful_transport()
  def answer(prompt):order.append('input');return 'Has my payment gone through?' if 'Synthetic' in prompt else 'CREATE ONE USD 10 SANDBOX ORDER'
  def admit(client):order.append('attestation_preflight');return True
  with patch('app._make_client',return_value=StubClient()),patch('app._confirm_standalone_demo',side_effect=admit),patch('builtins.input',side_effect=answer),patch('judge_sandbox_flow.UrllibSandboxTransport',return_value=transport),patch('judge_sandbox_flow.load_owner_sandbox_credentials',return_value=self.flow.creds),contextlib.redirect_stdout(io.StringIO()):
   self.assertEqual(main(self.args()+['--standalone']),0)
  self.assertEqual(order,['input','input','attestation_preflight'])
 def test_real_client_attributes_are_recorded_with_mocked_runtime_only(self):
  policy=StableSwapBaseline();policy.baseline_mib=123.0
  client=GraniteClient(lock_path=self.flow.root/'fixture.lock',stable_swap_policy=policy)
  sample={'memory_pressure_free_percent':65,'swap_used_mib':123,'load_1m':1,'disk_free_bytes':100*1024**3,'queue_counts':{'queue':0,'running':0}}
  choice=json.dumps({'order_id':fixture.ORDER_ID,'question_intent':'payment_status','phrase_id':'status_first','style_id':'concise'})
  result={'code':choice,'metrics':{},'wall_seconds':0.1,'resource_samples':[]}
  with patch.object(client,'preflight',return_value={'model_digests':{MODEL:MODEL_DIGEST}}),patch.object(policy,'check'),patch('local_model.guard.generate',return_value=result) as generated,patch('local_model.guard.get_json',return_value={'models':[]}),patch('local_model.guard.resources',return_value=sample),patch('local_model.time.sleep'):
   self.flow.run_flow(self.flow.successful_transport(),client=client)
  model=self.flow.journal_data()['model']
  self.assertTrue(client.execution_verified);self.assertTrue(model['executionVerified'])
  self.assertIs(client.stable_swap_policy,policy);self.assertEqual(model['guardPolicy'],'stable_swap_baseline');self.assertEqual(model['baselineMiB'],123.0)
  self.assertEqual(model['expectedModel'],MODEL);self.assertEqual(model['expectedDigest'],MODEL_DIGEST)
  self.assertEqual(model['startedAt'],'2026-10-31T00:00:00.000Z');self.assertEqual(model['completedAt'],'2026-10-31T00:00:00.000Z')
  self.assertEqual((model['questionIntent'],model['phraseId'],model['styleId']),('payment_status','status_first','concise'))
  self.assertEqual(generated.call_count,1);self.assertTrue(policy.closed)

if __name__=='__main__':unittest.main()
