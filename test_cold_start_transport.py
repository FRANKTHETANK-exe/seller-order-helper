"""Cold-start transport regression using only an ephemeral local fake server."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch

import runtime_guard as guard


@contextmanager
def delayed_reply(delay):
    stop = threading.Event()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            calls.append((self.path, json.loads(body)))
            if stop.wait(delay):
                return
            body = b'{"response":"synthetic selection","done":true}\n'
            try:
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={'poll_interval':0.05}, daemon=True)
    real_connection = http.client.HTTPConnection

    def local_connection(host, port, **kwargs):
        if (host, port) != ('127.0.0.1', 11434):
            raise AssertionError('Unexpected transport destination')
        return real_connection('127.0.0.1', server.server_port, **kwargs)

    sample = {'memory_pressure_free_percent':80, 'swap_used_mib':0,
              'resource_platform':'linux', 'memory_available_bytes':8 * guard.GIB,
              'memory_total_bytes':10 * guard.GIB,
              'load_1m':0, 'disk_free_bytes':50 * guard.GIB}
    thread.start()
    try:
        with patch('runtime_guard.http.client.HTTPConnection', side_effect=local_connection), \
             patch('runtime_guard.local_resources', return_value=sample), \
             patch('runtime_guard.get_json', return_value={'models':[]}):
            yield calls
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ColdStartTransportTests(unittest.TestCase):
    def test_headers_after_fifteen_seconds_fit_existing_request_budget(self):
        with delayed_reply(16) as calls:
            result = guard.generate('granite4.2:3b', '{}', standalone_demo=True)
        self.assertEqual(result['code'], 'synthetic selection')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], '/api/generate')
        self.assertGreaterEqual(result['wall_seconds'], 15)
        self.assertLess(result['wall_seconds'], guard.request_budget_seconds(standalone_demo=True))
        self.assertGreater(len(result['resource_samples']), 0)

    def test_selected_longer_profile_covers_load_and_keeps_monitoring(self):
        # A scaled profile proves the selected allowance, not the shorter
        # controller default, governs cold headers while resource checks run.
        with delayed_reply(2.2) as calls, \
             patch.dict(guard.LIMITS, {'request_seconds':0.2}), \
             patch.object(guard, 'request_budget_seconds', return_value=3) as budget:
            result = guard.generate('granite4.2:3b', '{}', standalone_demo=True)
        self.assertEqual(budget.call_count, 1)
        self.assertEqual(result['request_budget_seconds'], 3)
        self.assertGreater(result['wall_seconds'], 0.2)
        self.assertLess(result['wall_seconds'], 3)
        self.assertGreater(len(result['resource_samples']), 0)
        self.assertEqual(len(calls), 1)

    def test_existing_wall_deadline_still_cancels_delayed_headers_without_retry(self):
        with delayed_reply(2) as calls, patch.object(guard, 'request_budget_seconds', return_value=0.2):
            started = time.monotonic()
            with self.assertRaises(guard.PilotStop) as caught:
                guard.generate('granite4.2:3b', '{}', standalone_demo=True)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5)
        self.assertEqual(caught.exception.stage, 'model_request')
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
