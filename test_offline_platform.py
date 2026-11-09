"""Controlled platform/import fixtures; not actual Windows execution."""
import builtins,contextlib,io,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from app import _make_client,main,AppError
from local_model import GraniteClient,StubClient,LocalModelHeld
from sandbox_review_ui import ReviewSession

class OfflinePlatformTests(unittest.TestCase):
 def test_offline_dispatch_works_when_fcntl_cannot_import(self):
  original=builtins.__import__;calls=[]
  def guarded(name,*a,**kw):
   if name=='fcntl':calls.append(name);raise ModuleNotFoundError('fixture unavailable')
   return original(name,*a,**kw)
  def serve(client,*a,**kw):
   self.assertIsInstance(client,StubClient)
   state=ReviewSession(client).generate('Where is my tracking?')
   self.assertEqual(state['review_status'],'pending_human_review')
   self.assertEqual(state['external_action'],'none')
  with patch('builtins.__import__',side_effect=guarded),patch('sandbox_review_ui.serve_review_ui',side_effect=serve) as ui,contextlib.redirect_stdout(io.StringIO()):
   self.assertEqual(main(['sandbox-order']),0)
   self.assertEqual(main(['--client','stub','sandbox-review-ui','--port','0']),0)
   with self.assertRaises(SystemExit) as help_result:main(['--help'])
   self.assertEqual(help_result.exception.code,0)
  ui.assert_called_once();self.assertEqual(calls,[])
 def test_fresh_import_does_not_request_fcntl_for_stub(self):
  import subprocess
  script="""import builtins,subprocess,json,pathlib
original=builtins.__import__
def guard(name,*args,**kwargs):
 if name=='fcntl':raise AssertionError('stub attempted POSIX import')
 return original(name,*args,**kwargs)
builtins.__import__=guard
from local_model import StubClient
from sandbox_review_ui import ReviewSession
state=ReviewSession(StubClient()).generate('Has my payment gone through?')
assert state['external_action']=='none'
print('fresh stub import passed')
"""
  run=subprocess.run([sys.executable,'-B','-c',script],cwd=Path(__file__).parent,capture_output=True,text=True,timeout=15)
  self.assertEqual(run.returncode,0,run.stderr)
  self.assertIn('fresh stub import passed',run.stdout)
 def test_windows_live_client_fails_before_any_lock_or_health_call(self):
  with tempfile.TemporaryDirectory() as tmp:
   lock=Path(tmp)/'not-created'/'model.lock'
   with patch('local_model.sys.platform','win32'),patch('local_model.guard.inspect') as inspect,patch('local_model.guard.get_json') as health:
    with self.assertRaises(LocalModelHeld) as error:GraniteClient(lock_path=lock)
    self.assertEqual(error.exception.failure_code,'MODEL_PLATFORM_UNSUPPORTED')
    with self.assertRaisesRegex(AppError,'unsupported on this platform'):_make_client('granite')
    self.assertIsInstance(_make_client('stub'),StubClient)
   inspect.assert_not_called();health.assert_not_called();self.assertFalse(lock.parent.exists())
 def test_missing_posix_lock_never_falls_back_to_unlocked_live_execution(self):
  original=builtins.__import__
  def guarded(name,*a,**kw):
   if name=='fcntl':raise ModuleNotFoundError('fixture unavailable')
   return original(name,*a,**kw)
  with patch('builtins.__import__',side_effect=guarded):
   with self.assertRaises(LocalModelHeld) as error:GraniteClient()
  self.assertEqual(error.exception.failure_code,'MODEL_PLATFORM_UNSUPPORTED')
 @unittest.skipUnless(sys.platform != "win32", "POSIX lock module unavailable")
 def test_supported_client_retains_real_posix_lock_module(self):
  import fcntl
  with tempfile.TemporaryDirectory() as tmp:
   client=GraniteClient(lock_path=Path(tmp)/'model.lock')
   self.assertIs(client._lock_module,fcntl)

 def test_platform_refusal_remains_specific_in_review_session(self):
  # Inject a lazy factory to exercise the session error boundary; actual Windows
  # real-client construction refuses BEFORE UI startup and remains unsupported.
  def unsupported_client(prompt):
   return GraniteClient()(prompt)
  with patch('local_model.sys.platform','win32'),patch('local_model.guard.inspect') as inspect,patch('local_model.guard.get_json') as health:
   session=ReviewSession(unsupported_client)
   with self.assertRaises(ValueError):session.generate('Has my payment gone through?')
   self.assertEqual(session.state()['generation_failure'],{'stage':'admission','code':'MODEL_PLATFORM_UNSUPPORTED','request_attempted':False,'attempt_count':0})
   self.assertIsNone(session.state()['draft'])
   self.assertEqual(session.state()['external_action'],'none')
   inspect.assert_not_called();health.assert_not_called()
  for code in ('SWAP_BASELINE_INVALID','SWAP_BASELINE_DRIFT'):
   with self.subTest(code=code):
    def refused(prompt):
     raise LocalModelHeld('fixture private sentinel',stage='resource_guard',failure_code=code)
    session=ReviewSession(refused)
    with self.assertRaises(ValueError):session.generate('Has my payment gone through?')
    self.assertEqual(session.state()['generation_failure'],{'stage':'resource_guard','code':code,'request_attempted':False,'attempt_count':0})
    self.assertNotIn('fixture private sentinel',str(session.state()))

if __name__=='__main__':unittest.main()
