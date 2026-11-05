"""Offline loopback-handler I/O checks using socket pairs, with no port bind."""
import socket
import threading
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_model import StubClient
from sandbox_review_ui import ReviewSession, make_handler
import sandbox_review_ui


class ReviewRequestTimeoutTests(unittest.TestCase):
    def _handle(self, server_socket, session, errors):
        try:
            make_handler(session)(server_socket, ('127.0.0.1', 12345),
                                  SimpleNamespace(server_port=8765))
        except Exception as error:
            errors.append(error)

    def test_partial_post_times_out_without_consuming_generation(self):
        session = ReviewSession(StubClient())
        errors = []
        client, server = socket.socketpair()
        with client, server:
            with patch.object(sandbox_review_ui, 'REQUEST_IO_TIMEOUT_SECONDS', 0.05):
                worker = threading.Thread(target=self._handle,
                                          args=(server, session, errors), daemon=True)
                worker.start()
                client.sendall(
                    b'POST /api/generate HTTP/1.1\r\n'
                    b'Host: 127.0.0.1:8765\r\n'
                    b'Origin: http://127.0.0.1:8765\r\n'
                    b'Content-Type: application/json\r\n'
                    b'Content-Length: 100\r\n'
                    b'Connection: close\r\n\r\n{')
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive(), 'partial body held a server thread')
        self.assertEqual(errors, [])
        self.assertFalse(session.attempted)
        self.assertFalse(session.refresh_attempted)
        self.assertIsNone(session.suggestion)

    def test_partial_headers_time_out_before_any_session_action(self):
        session = ReviewSession(StubClient())
        errors = []
        client, server = socket.socketpair()
        with client, server:
            with patch.object(sandbox_review_ui, 'REQUEST_IO_TIMEOUT_SECONDS', 0.05):
                worker = threading.Thread(target=self._handle,
                                          args=(server, session, errors), daemon=True)
                worker.start()
                client.sendall(b'GET /api/state HTTP/1.1\r\nHost: 127')
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive(), 'partial headers held a server thread')
        self.assertEqual(errors, [])
        self.assertFalse(session.attempted)
        self.assertFalse(session.refresh_attempted)

    def test_dripped_body_hits_total_read_deadline(self):
        session = ReviewSession(StubClient())
        errors = []
        client, server = socket.socketpair()
        with client, server:
            with patch.object(sandbox_review_ui, 'REQUEST_IO_TIMEOUT_SECONDS', 0.12), \
                 patch.object(sandbox_review_ui, 'REQUEST_TOTAL_READ_SECONDS', 0.18):
                worker = threading.Thread(target=self._handle,
                                          args=(server, session, errors), daemon=True)
                worker.start()
                client.sendall(
                    b'POST /api/generate HTTP/1.1\r\n'
                    b'Host: 127.0.0.1:8765\r\n'
                    b'Origin: http://127.0.0.1:8765\r\n'
                    b'Content-Type: application/json\r\n'
                    b'Content-Length: 100\r\n'
                    b'Connection: close\r\n\r\n')
                for _ in range(3):
                    time.sleep(0.05)
                    client.sendall(b' ')
                worker.join(timeout=0.5)
                self.assertFalse(worker.is_alive(), 'dripped body exceeded deadline')
        self.assertEqual(errors, [])
        self.assertFalse(session.attempted)
        self.assertIsNone(session.suggestion)

    def test_dripped_headers_hit_total_read_deadline(self):
        session = ReviewSession(StubClient())
        errors = []
        client, server = socket.socketpair()
        with client, server:
            with patch.object(sandbox_review_ui, 'REQUEST_IO_TIMEOUT_SECONDS', 0.12), \
                 patch.object(sandbox_review_ui, 'REQUEST_TOTAL_READ_SECONDS', 0.18):
                worker = threading.Thread(target=self._handle,
                                          args=(server, session, errors), daemon=True)
                worker.start()
                client.sendall(b'GET /api/state HTTP/1.1\r\nHost: 127')
                for _ in range(3):
                    time.sleep(0.05)
                    client.sendall(b' ')
                worker.join(timeout=0.5)
                self.assertFalse(worker.is_alive(), 'dripped headers exceeded deadline')
        self.assertEqual(errors, [])
        self.assertFalse(session.attempted)
        self.assertFalse(session.refresh_attempted)

    def test_complete_read_only_request_still_responds(self):
        session = ReviewSession(StubClient())
        errors = []
        client, server = socket.socketpair()
        with client, server:
            with patch.object(sandbox_review_ui, 'REQUEST_IO_TIMEOUT_SECONDS', 0.5):
                worker = threading.Thread(target=self._handle,
                                          args=(server, session, errors), daemon=True)
                worker.start()
                client.sendall(
                    b'GET /api/state HTTP/1.1\r\n'
                    b'Host: 127.0.0.1:8765\r\n'
                    b'Connection: close\r\n\r\n')
                response = bytearray()
                client.settimeout(1)
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if b'"external_action":"none"' in response:
                        break
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIn(b'HTTP/1.0 200 OK', response)
        self.assertIn(b'"external_action":"none"', response)

    def test_complete_post_still_generates_one_offline_draft(self):
        client_model = StubClient()
        session = ReviewSession(client_model)
        errors = []
        body = json.dumps({'question': 'Has my payment gone through?'}).encode()
        client, server = socket.socketpair()
        with client, server:
            with patch.object(sandbox_review_ui, 'REQUEST_IO_TIMEOUT_SECONDS', 0.5):
                worker = threading.Thread(target=self._handle,
                                          args=(server, session, errors), daemon=True)
                worker.start()
                client.sendall(
                    b'POST /api/generate HTTP/1.1\r\n'
                    b'Host: 127.0.0.1:8765\r\n'
                    b'Origin: http://127.0.0.1:8765\r\n'
                    b'Content-Type: application/json\r\n'
                    + f'Content-Length: {len(body)}\r\n'.encode()
                    + b'Connection: close\r\n\r\n' + body)
                response = bytearray()
                client.settimeout(1)
                while b'"review_status":"pending_human_review"' not in response:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIn(b'HTTP/1.0 200 OK', response)
        self.assertIn(b'"review_status":"pending_human_review"', response)
        self.assertTrue(session.attempted)
        self.assertEqual(client_model.last_run['inference_requests_attempted'], 0)

    def test_read_deadline_does_not_cover_local_draft_computation(self):
        class SlowStub(StubClient):
            def __call__(self, prompt):
                time.sleep(0.12)
                return super().__call__(prompt)

        session = ReviewSession(SlowStub())
        errors = []
        body = b'{"question":"Has my payment gone through?"}'
        client, server = socket.socketpair()
        with client, server:
            with patch.object(sandbox_review_ui, 'REQUEST_IO_TIMEOUT_SECONDS', 0.5), \
                 patch.object(sandbox_review_ui, 'REQUEST_TOTAL_READ_SECONDS', 0.05):
                worker = threading.Thread(target=self._handle,
                                          args=(server, session, errors), daemon=True)
                worker.start()
                client.sendall(
                    b'POST /api/generate HTTP/1.1\r\n'
                    b'Host: 127.0.0.1:8765\r\n'
                    b'Origin: http://127.0.0.1:8765\r\n'
                    b'Content-Type: application/json\r\n'
                    + f'Content-Length: {len(body)}\r\n'.encode()
                    + b'Connection: close\r\n\r\n' + body)
                response = bytearray()
                client.settimeout(1)
                while b'"review_status":"pending_human_review"' not in response:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIn(b'HTTP/1.0 200 OK', response)
        self.assertTrue(session.attempted)


if __name__ == '__main__':
    unittest.main()
