"""Offline profile/transport checks, never contacting existing model services."""
import io
import json
import threading
import unittest
from unittest.mock import patch

import runtime_guard as guard


class LinuxColdStartBudgetTests(unittest.TestCase):
    def test_longer_profile_is_only_exact_linux_standalone(self):
        for platform in ('linux', 'darwin', 'win32'):
            for standalone in (False, True, 1, 'yes', None):
                with self.subTest(platform=platform, standalone=standalone), \
                     patch.object(guard.sys, 'platform', platform):
                    expected = 180 if platform == 'linux' and standalone is True else 60
                    self.assertEqual(guard.request_budget_seconds(
                        standalone_demo=standalone), expected)
        self.assertEqual(guard.LIMITS['request_seconds'], 60)
        self.assertEqual(guard.LIMITS['total_seconds'], 90)

    def test_transport_watchdog_and_receipt_share_the_resolved_budget(self):
        for platform, standalone, expected in (
                ('linux', True, 180), ('linux', False, 60), ('darwin', True, 60)):
            class Response(io.BytesIO):
                status = 200

            class Connection:
                sock = None
                received_timeout = None
                sent = []

                def __init__(self, host, port, timeout):
                    self.assert_destination = (host, port)
                    Connection.received_timeout = timeout

                def request(self, method, path, body, headers):
                    Connection.sent.append((method, path, json.loads(body)))

                def getresponse(self):
                    return Response(b'{"response":"synthetic selection","done":true}\n')

                def close(self):
                    pass

            real_timer = threading.Timer
            with self.subTest(platform=platform, standalone=standalone), \
                 patch.object(guard.sys, 'platform', platform), \
                 patch.object(guard.http.client, 'HTTPConnection', Connection), \
                 patch.object(guard.threading, 'Timer', wraps=real_timer) as timer:
                result = guard.generate('granite4.2:3b', '{}', standalone_demo=standalone)
            self.assertEqual(Connection.received_timeout, expected)
            self.assertEqual(timer.call_args.args[0], expected)
            self.assertEqual(result['request_budget_seconds'], expected)
            self.assertEqual(len(Connection.sent), 1)
            method, path, payload = Connection.sent[0]
            self.assertEqual((method, path), ('POST', '/api/generate'))
            self.assertEqual(payload['keep_alive'], 0)
            self.assertEqual(payload['options'], guard.GENERATION_OPTIONS)
            self.assertFalse(payload['think'])

class StreamElapsedBudgetTests(unittest.TestCase):
    def test_stream_elapsed_checks_enforce_selected_budget_without_timer(self):
        for standalone, elapsed, should_pass in ((True, 61, True),
                                                 (True, 181, False),
                                                 (False, 61, False)):
            class Response(io.BytesIO):
                status = 200

            class Connection:
                sock = None
                sent = 0

                def __init__(self, *_args, **_kwargs):
                    pass

                def request(self, *_args, **_kwargs):
                    Connection.sent += 1

                def getresponse(self):
                    return Response(b'{"response":"selection","done":true}\n')

                def close(self):
                    pass

            with self.subTest(standalone=standalone, elapsed=elapsed), \
                 patch.object(guard.sys, 'platform', 'linux'), \
                 patch.object(guard.http.client, 'HTTPConnection', Connection), \
                 patch.object(guard.threading, 'Timer'), \
                 patch.object(guard.time, 'monotonic', side_effect=[0, elapsed, elapsed]):
                if should_pass:
                    result = guard.generate('granite4.2:3b', '{}', standalone_demo=standalone)
                    self.assertEqual(result['request_budget_seconds'], 180)
                    self.assertEqual(result['wall_seconds'], elapsed)
                else:
                    with self.assertRaises(guard.PilotStop) as caught:
                        guard.generate('granite4.2:3b', '{}', standalone_demo=standalone)
                    self.assertEqual(caught.exception.failure_code, 'MODEL_REQUEST_FAILED')
            self.assertEqual(Connection.sent, 1)


if __name__ == '__main__':
    unittest.main()
