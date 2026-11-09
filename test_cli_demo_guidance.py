"""The facts-only CLI points to a working, explicit offline preview."""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import unittest
from unittest.mock import patch
import app


class CliDemoGuidanceTests(unittest.TestCase):
    def test_facts_view_points_to_working_offline_draft_without_live_dependencies(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('Offline guidance invoked a live dependency')

        with ExitStack() as stack:
            for target in ('sandbox_adapter.UrllibSandboxTransport.request',
                           'sandbox_adapter.load_owner_sandbox_credentials',
                           'runtime_guard.inspect', 'runtime_guard.inspect_standalone',
                           'runtime_guard.generate', 'runtime_guard.get_json'):
                stack.enter_context(patch(target, side_effect=forbidden))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(app.main(['sandbox-review']), 0)
            guidance = output.getvalue()
            self.assertNotIn('NO SANDBOX REPLY DRAFT IS AVAILABLE IN THIS APP', guidance)
            self.assertIn('python3 -B app.py --client stub sandbox-ai-review', guidance)
            self.assertIn('synthetic', guidance)

            preview = StringIO()
            with redirect_stdout(preview), patch('builtins.input', return_value=''):
                self.assertEqual(app.main(['--client', 'stub', 'sandbox-ai-review']), 0)
            text = preview.getvalue()
            self.assertIn('OFFLINE REVIEW PREVIEW — NO MODEL INFERENCE; NOT SENT', text)
            self.assertIn('HUMAN REVIEW PENDING', text)
            self.assertIn('not a PayPal lookup', text)
            self.assertNotIn('Local human review recorded', text)


if __name__ == '__main__':
    unittest.main()
