"""Loopback-only browser UI for reviewing the one recorded sandbox order.

This module reads the validated local receipt, uses the existing bounded local
model client only after an operator clicks Generate, and records review in
memory for this server session. It has no PayPal transport or send/action route.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

from sandbox_ai_review import (facts_from_receipt, generate_sandbox_review,
                               normalize_payment_question,
                               facts_from_verified_live_read, record_human_review,
                               LIVE_SOURCES, SandboxOrderFacts, validate_review_draft)
from sandbox_reconciliation import read_recorded_evidence

PAGE = Path(__file__).with_name('sandbox-review-ui.html')
MAX_BODY = 4096
REQUEST_IO_TIMEOUT_SECONDS = 10
REQUEST_TOTAL_READ_SECONDS = 10


class _DeadlineReader:
    """Bound total header/body read time, even if a client keeps dripping bytes."""

    def __init__(self, reader, connection):
        self._reader = reader
        self._connection = connection
        self._deadline = time.monotonic() + REQUEST_TOTAL_READ_SECONDS

    def _bounded(self, operation):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Local review request read deadline exceeded')
        self._connection.settimeout(min(REQUEST_IO_TIMEOUT_SECONDS, remaining))
        try:
            return operation()
        finally:
            # A later local model computation is not subject to the read deadline.
            self._connection.settimeout(REQUEST_IO_TIMEOUT_SECONDS)

    def readline(self, limit=-1):
        result = bytearray()
        while limit < 0 or len(result) < limit:
            available = self._bounded(lambda: self._reader.peek(1))
            if not available:
                break
            take = available if limit < 0 else available[:limit - len(result)]
            newline = take.find(b'\n')
            if newline >= 0:
                take = take[:newline + 1]
            result.extend(self._reader.read(len(take)))
            if newline >= 0:
                break
        return bytes(result)

    def read(self, size=-1):
        if size < 0:
            raise ValueError('Bounded request read requires a size')
        result = bytearray()
        while len(result) < size:
            chunk = self._bounded(lambda: self._reader.read1(size - len(result)))
            if not chunk:
                break
            result.extend(chunk)
        return bytes(result)

    def close(self):
        self._reader.close()


class ReviewUIError(ValueError):
    pass


class ReviewSession:
    """One saved receipt or one pinned live read, plus at most one model attempt."""

    def __init__(self, client, *, refresh_known_order=None, reviewer_pin_path=None,
                 clock=None, live_max_age_seconds=300, _initial_facts=None):
        if refresh_known_order is not None and not callable(refresh_known_order):
            raise TypeError('Live refresh must be an explicit callable')
        if type(live_max_age_seconds) is not int or not 1 <= live_max_age_seconds <= 600:
            raise ValueError('Live fact age bound is invalid')
        if _initial_facts is not None and type(_initial_facts) is not SandboxOrderFacts:
            raise TypeError('Completed review requires exact validated order facts')
        self.facts = (replace(_initial_facts) if _initial_facts is not None
                      else facts_from_receipt(read_recorded_evidence()))
        self.client = client
        self._refresh_known_order = refresh_known_order
        self._reviewer_pin_path = Path(reviewer_pin_path) if reviewer_pin_path is not None else None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._live_max_age_seconds = live_max_age_seconds
        self.refresh_attempted = False
        self.refresh_failure = None
        self.refresh_call_counts = None
        self.attempted = False
        self.generation_failure = None
        self.suggestion = None
        self.review_result = None
        self.question_version = 0
        self.lock = threading.Lock()

    def _offline_questions_enabled(self):
        from local_model import StubClient
        return (type(self) is ReviewSession and type(self.client) is StubClient
                and self.facts.source == 'synthetic_order_fixture'
                and self._refresh_known_order is None)

    def _check_question_version(self, version):
        # Original one-shot clients need no version. After an explicit reset,
        # every draft/review request must identify the current local question.
        if version is None and self.question_version == 0:
            return
        if type(version) is not int or version != self.question_version:
            raise ReviewUIError('This question changed in another tab. Reload session status before continuing.')

    def new_question(self, question_version):
        """Clear a completed synthetic stub example; never reset a model/read limit."""
        with self.lock:
            if not self._offline_questions_enabled():
                raise ReviewUIError('New questions here are available only for synthetic offline template sessions.')
            if type(question_version) is not int:
                raise ReviewUIError('Reload the current question before starting another.')
            self._check_question_version(question_version)
            if self.suggestion is None or self.generation_failure is not None:
                raise ReviewUIError('Finish and inspect this offline draft before starting another question.')
            self.question_version += 1
            self.attempted = False
            self.generation_failure = None
            self.suggestion = None
            self.review_result = None
            return self.state()

    def _live_facts_fresh(self):
        if self.facts.source not in LIVE_SOURCES:
            return False
        try:
            observed = datetime.fromisoformat(self.facts.observed_at.replace('Z', '+00:00'))
            now = self._clock()
            if now.tzinfo is None:
                return False
            age = (now.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
            return -30 <= age <= self._live_max_age_seconds
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False

    def _read_status(self):
        if self._refresh_known_order is None:
            return 'disabled'
        if self.refresh_failure is not None:
            return 'failed'
        if not self.refresh_attempted:
            return 'not_requested'
        return 'succeeded' if self._live_facts_fresh() else 'stale'

    def refresh(self):
        """Use the injected owner-only reader at most once; never retry in-session."""
        with self.lock:
            if self._refresh_known_order is None:
                raise ReviewUIError('Live sandbox refresh is not enabled for this session.')
            if self.refresh_attempted:
                raise ReviewUIError('This session already used its one sandbox refresh attempt.')
            self.refresh_attempted = True
            try:
                value = self._refresh_known_order()
                counts = value.get('safe_call_counts') if type(value) is dict else None
                self.refresh_call_counts = self._safe_call_counts(counts)
                try:
                    facts = facts_from_verified_live_read(
                        value, pin_path=self._reviewer_pin_path)
                except Exception:
                    self.refresh_failure = {
                        'stage': 'live_fact_reconciliation',
                        'code': 'LIVE_FACT_RECONCILIATION_FAILED',
                    }
                    if self.refresh_call_counts is not None:
                        self.refresh_failure['call_counts'] = dict(self.refresh_call_counts)
                    raise ReviewUIError('The live facts did not match the pinned-order read; no draft was generated.') from None
                self.facts = facts
                if not self._live_facts_fresh():
                    self.facts = facts_from_receipt(read_recorded_evidence())
                    self.refresh_failure = {'stage': 'freshness', 'code': 'STALE_LIVE_READ'}
                    raise ReviewUIError('The live read was stale; the saved receipt remains historical.')
                self.refresh_failure = None
                self.suggestion = None
                self.review_result = None
                return self.state()
            except ReviewUIError:
                raise
            except Exception as error:
                stage = getattr(error, 'stage', 'live_read')
                code = getattr(error, 'failure_code', 'LIVE_READ_REJECTED')
                stage = stage if type(stage) is str and re.fullmatch(r'[A-Za-z0-9_]{1,32}', stage) else 'live_read'
                code = code if type(code) is str and re.fullmatch(r'[A-Z0-9_]{1,64}', code) else 'LIVE_READ_REJECTED'
                self.refresh_failure = {'stage': stage, 'code': code}
                http_status = getattr(error, 'http_status', None)
                if type(http_status) is int and 100 <= http_status <= 599:
                    self.refresh_failure['http_status'] = http_status
                counts = getattr(error, 'safe_call_counts', None)
                self.refresh_call_counts = self._safe_call_counts(
                    counts if counts is not None else self.refresh_call_counts)
                if self.refresh_call_counts is not None:
                    self.refresh_failure['call_counts'] = dict(self.refresh_call_counts)
                # Keep the last validated receipt on screen as historical evidence.
                self.facts = facts_from_receipt(read_recorded_evidence())
                raise ReviewUIError('Order refresh failed validation. No message response was generated.') from None

    @staticmethod
    def _safe_call_counts(value):
        """Accept only the two fixed one-shot counters; never expose transport data."""
        if type(value) is not dict or set(value) != {'oauth_attempts', 'pinned_order_get_attempts'}:
            return None
        if any(type(value[key]) is not int or value[key] not in (0, 1)
               for key in value):
            return None
        return {'oauth_attempts': value['oauth_attempts'],
                'pinned_order_get_attempts': value['pinned_order_get_attempts']}

    def state(self):
        result = {
            'source': self.facts.source,
            'observed_at': self.facts.observed_at,
            'order_id': self.facts.order_id,
            'order_status': self.facts.order_status,
            'payment_truth': self.facts.payment_truth,
            'capture_status': self.facts.capture_status,
            'currency': self.facts.currency,
            'amount': self.facts.amount,
            'client_kind': getattr(self.client, 'kind', 'unknown'),
            'model_name': getattr(self.client, 'model_name', 'unknown'),
            'standalone_demo': getattr(self.client, 'standalone_demo', False) is True,
            'controller_queue_visibility': ('not_checked_standalone_mode'
                if getattr(self.client, 'standalone_demo', False) is True else 'checked'),
            'live_refresh_enabled': self._refresh_known_order is not None,
            'live_refresh_status': self._read_status(),
            'live_refresh_failure': dict(self.refresh_failure) if self.refresh_failure else None,
            'live_refresh_call_counts': (dict(self.refresh_call_counts)
                                         if self.refresh_call_counts is not None else None),
            'live_facts_fresh': self._live_facts_fresh(),
            'generation_attempted': self.attempted,
            'question_version': self.question_version,
            'new_question_enabled': self._offline_questions_enabled(),
            'generation_failure': dict(self.generation_failure) if self.generation_failure else None,
            'draft': None,
            'review_status': 'not_generated',
            'external_action': 'none',
        }
        if self.suggestion is not None:
            result['draft'] = {
                'text': self.suggestion.review_text,
                'digest': self.suggestion.review_digest,
                'phrase_id': self.suggestion.phrase_id,
                'style_id': self.suggestion.style_id,
                'question_intent': self.suggestion.question_intent,
                'facts_fingerprint': self.suggestion.facts_fingerprint,
                'human_review_required': True,
                'sent': False,
                'seller_next_step': self.suggestion.seller_next_step,
                'seller_attention': list(self.suggestion.seller_attention),
            }
            result['review_status'] = ('reviewed_not_sent' if self.review_result
                                       else 'pending_human_review')
        return result

    def generate(self, buyer_question='Has my payment gone through?', *, question_version=None):
        with self.lock:
            self._check_question_version(question_version)
            if self._refresh_known_order is not None:
                if self.refresh_failure is not None:
                    raise ReviewUIError('The order check failed. Refresh is required before preparing a message response.')
                if not self.refresh_attempted:
                    raise ReviewUIError('Refresh the pinned sandbox order before generating this live-review draft.')
                if not self._live_facts_fresh():
                    raise ReviewUIError('The live read is stale; restart the session before generating a draft.')
            if self.attempted:
                raise ReviewUIError('This review session already used its one generation attempt.')
            try:
                buyer_question = normalize_payment_question(buyer_question)
            except Exception as error:
                raise ReviewUIError(str(error)) from None
            self.attempted = True
            try:
                self.suggestion = generate_sandbox_review(self.facts, self.client, buyer_question)
            except Exception as error:
                stage = getattr(error, 'stage', None)
                code = getattr(error, 'failure_code', None)
                attempted = getattr(error, 'request_attempted', None)
                count = getattr(error, 'attempt_count', None)
                allowed_stages = {'tag_lookup', 'digest_check', 'admission', 'resource_guard',
                                  'model_request', 'response_parse', 'selection_contract'}
                allowed_codes = {'MODEL_TAG_LOOKUP_FAILED', 'MODEL_TAG_UNAVAILABLE',
                    'MODEL_DIGEST_UNAVAILABLE', 'MODEL_DIGEST_MISMATCH', 'ADMISSION_LOCK_FAILED',
                    'ADMISSION_CHECK_FAILED', 'RESOURCE_ADMISSION_DENIED', 'SWAP_NONZERO', 'MODEL_REQUEST_FAILED',
                    'MODEL_HTTP_ERROR', 'MODEL_RESPONSE_INVALID', 'SELECTION_CONTRACT_REJECTED',
                    'MODEL_SETUP_ERROR', 'MODEL_PLATFORM_UNSUPPORTED', 'SWAP_BASELINE_INVALID',
                    'SWAP_BASELINE_DRIFT'}
                if (stage not in allowed_stages or code not in allowed_codes
                        or type(attempted) is not bool or count not in (0, 1)):
                    stage, code, attempted, count = 'model_request', 'MODEL_SETUP_ERROR', None, None
                failure = {'stage': stage, 'code': code,
                           'request_attempted': attempted, 'attempt_count': count}
                http_status = getattr(error, 'http_status', None)
                if type(http_status) is int and 100 <= http_status <= 599:
                    failure['http_status'] = http_status
                self.generation_failure = failure
                raise ReviewUIError('Draft generation stopped; no external action occurred.') from None
            return self.state()

    def review(self, digest, *, question_version=None):
        with self.lock:
            self._check_question_version(question_version)
            if self.facts.source in LIVE_SOURCES and not self._live_facts_fresh():
                raise ReviewUIError('The live facts are stale; this draft cannot be marked reviewed.')
            if self.suggestion is None:
                raise ReviewUIError('Generate and inspect a draft before recording review.')
            if self.review_result is not None:
                raise ReviewUIError('This exact draft has already been reviewed in this session.')
            try:
                self.review_result = record_human_review(self.suggestion, self.facts, digest)
            except Exception:
                raise ReviewUIError('Review did not match the current draft; nothing changed.') from None
            return self.state()


class CompletedReviewSession(ReviewSession):
    """Review the exact in-memory result of a completed one-shot judge flow."""

    def __init__(self, result, client, *, pin_path, journal_path, clock=None):
        from local_model import GraniteClient, MODEL, MODEL_DIGEST
        from reviewer_sandbox_order import read_judge_order_receipt
        if (type(client) is not GraniteClient or client.execution_verified is not True
                or client.last_run.get('status') != 'completed'
                or client.last_run.get('inference_requests_attempted') != 1
                or client.last_run.get('model') != MODEL
                or client.last_run.get('model_digest') != MODEL_DIGEST
                or client.last_run.get('cleanup_confirmed') is not True):
            raise ReviewUIError('A completed, verified local response is required')
        if type(result) is not dict or result.get('state') != 'draft_pending_human_review':
            raise ReviewUIError('A completed Sandbox draft is required')
        receipt = read_judge_order_receipt(pin_path=Path(pin_path), journal_path=Path(journal_path),
                                           require_completed_model=True)
        facts, suggestion = result.get('facts'), result.get('draft')
        if (type(facts) is not SandboxOrderFacts or facts.source != 'reviewer_live_paypal_sandbox_read'
                or facts.order_id != receipt.pin.order_id
                or facts.configuration_id != receipt.pin.configuration_id
                or facts.observed_at != receipt.observed_at
                or facts._pin_path is None
                or Path(facts._pin_path).absolute() != Path(pin_path).absolute()
                or Path(result.get('journal', '')).absolute() != Path(journal_path).absolute()):
            raise ReviewUIError('Completed draft does not match its private Sandbox receipt')
        validate_review_draft(suggestion, facts)
        super().__init__(client, reviewer_pin_path=pin_path, clock=clock, _initial_facts=facts)
        self.suggestion = suggestion
        self.attempted = True

    def refresh(self):
        raise ReviewUIError('This screen uses the completed Sandbox run; it cannot repeat the read')

    def generate(self, *args, **kwargs):
        raise ReviewUIError('This is the completed message response; no additional attempt is available')

    def new_question(self, *args, **kwargs):
        raise ReviewUIError('This completed run cannot start another question')

    def state(self):
        result = super().state()
        result.update(completed_flow=True, completed_buyer_question=self.suggestion.buyer_question,
                      model_execution_verified=True)
        return result


def make_handler(session: ReviewSession):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'SellerOrderReview/1'
        sys_version = ''

        def setup(self):
            # Bound both idle I/O and total header/body read time.
            self.request.settimeout(REQUEST_IO_TIMEOUT_SECONDS)
            super().setup()
            self.rfile = _DeadlineReader(self.rfile, self.request)

        def log_message(self, _format, *_args):
            # Do not persist request data, browser values, or order details.
            return

        def _host_ok(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def _send(self, status, body, content_type='application/json; charset=utf-8'):
            data = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy',
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, value):
            encoded = json.dumps(value, ensure_ascii=True, separators=(',', ':'))
            self._send(status, encoded)

        def do_GET(self):
            if not self._host_ok():
                return self._json(421, {'error': 'Use the printed loopback address.'})
            path = urlsplit(self.path).path
            if path == '/' and self.path == '/':
                try:
                    data = PAGE.read_bytes()
                except OSError:
                    return self._json(503, {'error': 'The local review page is unavailable.'})
                if len(data) > 32768:
                    return self._json(503, {'error': 'The local review page exceeded its size bound.'})
                return self._send(200, data, 'text/html; charset=utf-8')
            if self.path == '/api/state':
                return self._json(200, session.state())
            return self._json(404, {'error': 'Not found.'})

        def do_POST(self):
            if not self._host_ok():
                return self._json(421, {'error': 'Use the printed loopback address.'})
            origin = self.headers.get('Origin')
            expected_origin = f'http://127.0.0.1:{self.server.server_port}'
            if origin != expected_origin:
                return self._json(403, {'error': 'Same-origin review action required.'})
            if self.headers.get_content_type() != 'application/json':
                return self._json(415, {'error': 'JSON is required.'})
            try:
                size = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= size <= MAX_BODY:
                    raise ValueError
                request = json.loads(self.rfile.read(size))
                if type(request) is not dict:
                    raise ValueError
            except TimeoutError:
                # Do not process or replay a partial action request.
                self.close_connection = True
                return
            except (ValueError, TypeError, json.JSONDecodeError):
                return self._json(400, {'error': 'Invalid bounded request.'})
            try:
                if self.path == '/api/generate':
                    if (set(request) not in ({'question'}, {'question','question_version'}) or type(request['question']) is not str
                            or len(request['question']) > 500
                            or len(request['question'].encode('utf-8')) > 1200):
                        return self._json(400, {'error':'Enter one question of at most 500 characters.'})
                    if 'question_version' in request:
                        return self._json(200, session.generate(request['question'], question_version=request['question_version']))
                    return self._json(200, session.generate(request['question']))
                if self.path == '/api/new-question' and set(request) == {'question_version'}:
                    return self._json(200, session.new_question(request['question_version']))
                if self.path == '/api/refresh' and request == {}:
                    return self._json(200, session.refresh())
                if (self.path == '/api/review' and set(request) in ({'digest'}, {'digest','question_version'})
                        and type(request['digest']) is str and len(request['digest']) == 64):
                    if 'question_version' in request:
                        return self._json(200, session.review(request['digest'], question_version=request['question_version']))
                    return self._json(200, session.review(request['digest']))
            except ReviewUIError as error:
                if self.path == '/api/refresh':
                    return self._json(409, {'error': str(error),
                        'diagnostic': session.state().get('live_refresh_failure')})
                return self._json(409, {'error': str(error)})
            return self._json(404, {'error': 'Unknown review action.'})

        def do_OPTIONS(self):
            return self._json(405, {'error': 'Cross-origin requests are not supported.'})

    return Handler


def serve_review_ui(client, port=8765, *, refresh_known_order=None, reviewer_pin_path=None):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('Invalid local port')
    session = ReviewSession(client, refresh_known_order=refresh_known_order,
                            reviewer_pin_path=reviewer_pin_path)
    return _serve_session(session, port)


def serve_completed_review(result, client, *, pin_path, journal_path, port=0):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('Invalid local port')
    session = CompletedReviewSession(result, client, pin_path=pin_path, journal_path=journal_path)
    return _serve_session(session, port)


def _serve_session(session, port):
    server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(session))
    server.daemon_threads = True
    address = server.server_address
    print(f'Local review page: http://127.0.0.1:{address[1]}/')
    if isinstance(session, CompletedReviewSession):
        print('Review the exact draft from the completed Sandbox run. Opening this screen makes no new PayPal or model request.')
    elif session._refresh_known_order is None:
        print('Private loopback only. Synthetic example only; no PayPal request or outbound action.' if session.facts.source == 'synthetic_order_fixture' else 'Private loopback only. Saved receipt only; no PayPal request or outbound action.')
    else:
        print('Private loopback only. One explicit read-only Sandbox refresh is available after operator click; no order writes or outbound messages.')
    print('Press Ctrl-C to close. Review state is held in memory for this session only.')
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return session.state()
