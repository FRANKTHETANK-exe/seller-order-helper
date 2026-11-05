"""Windows-safe offline dispatch smoke. No live model, credentials or network.

Run from the extracted app directory: py -3 offline_smoke.py (Windows), or
python3 -B offline_smoke.py (POSIX). A pass is only on the OS actually running it.
The UI server is injected: this is dispatch/state validation, not browser QA.
"""
from contextlib import redirect_stdout
from io import StringIO
import json
import platform
import sys
from unittest.mock import patch
import app
from local_model import StubClient
from sandbox_review_ui import ReviewSession


def smoke():
    checks=[]
    def forbidden(*args,**kwargs):
        raise AssertionError('Live dependency called during offline smoke')
    def offline_ui(client,*args,**kwargs):
        assert isinstance(client,StubClient)
        state=ReviewSession(client).generate('Has my payment gone through?')
        assert state['external_action']=='none'
        assert state['review_status']=='pending_human_review'
        assert state['payment_truth']=='CREATED_NOT_APPROVED'
        assert client.last_run['inference_requests_attempted']==0
        checks.append('stub_dispatch_synthetic_state_no_external_action')
    with patch('runtime_guard.get_json',side_effect=forbidden), \
         patch('runtime_guard.inspect',side_effect=forbidden), \
         patch('runtime_guard.inspect_standalone',side_effect=forbidden), \
         patch('runtime_guard.generate',side_effect=forbidden), \
         patch('sandbox_adapter.UrllibSandboxTransport.request',side_effect=forbidden), \
         patch('sandbox_review_ui.serve_review_ui',side_effect=offline_ui) as server, \
         redirect_stdout(StringIO()):
        try:app.main(['--help'])
        except SystemExit as result:assert result.code==0
        else:raise AssertionError('Help did not exit normally')
        checks.append('help')
        assert app.main(['sandbox-order'])==0
        checks.append('synthetic_fixture_display')
        assert app.main(['--client','stub','sandbox-review-ui','--port','0'])==0
        server.assert_called_once()
    return {'scope':'offline help/synthetic fixture/stub dispatch only; fake UI server/live dependencies blocked',
            'actualOS':platform.system(),'python':sys.version.split()[0],
            'checks':checks,'networkOrModelCalls':0,'windowsVerified':platform.system()=='Windows'}

if __name__=='__main__':print(json.dumps(smoke(),indent=2))
