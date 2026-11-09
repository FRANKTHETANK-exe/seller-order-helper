"""Seller Order Helper with synthetic and read-only PayPal sandbox workflows.

Local suggestion and review workflows are synthetic by default. An explicit
Granite client can make one bounded local inference through local_model.py.
The separate sandbox reader is pinned to one known order and remains read-only;
its local AI review draft never changes or sends anything to PayPal.
No outbound message, create, approval, capture, refund, or payment operation
is implemented here.
"""
from __future__ import annotations

import argparse
from functools import partial
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
from uuid import uuid4

from facts import AMOUNT, CURRENCY, PRODUCT_ID, PRODUCT_NAME, SELLER_ID, OrderFacts
from suggestions import ReplySuggestion, SuggestionError, fingerprint_order_facts, generate_suggestion

APP_ID = 0x53494D33
DB_VERSION = 3
DEFAULT_DB = Path(__file__).resolve().parent / 'data' / 'simulated-orders-v3.sqlite3'
STATUSES = ('CREATED', 'APPROVED', 'COMPLETED', 'VOIDED')
NEXT = {'CREATED': ('APPROVED', 'VOIDED'), 'APPROVED': ('COMPLETED', 'VOIDED'),
        'COMPLETED': (), 'VOIDED': ()}
ORDER_COLUMNS = ('id', 'mode', 'source', 'buyer', 'seller_id', 'product_id', 'product_name',
                 'currency', 'amount', 'status', 'capture_status', 'revision', 'created_at', 'updated_at')
DRAFT_COLUMNS = ('order_id', 'suggestion_id', 'order_revision', 'category', 'phrase_id', 'style_id',
                 'facts_fingerprint', 'reply_text', 'approved_body', 'content_digest', 'client_kind',
                 'model_name', 'model_execution_verified', 'client_metadata', 'created_at',
                 'reviewed_at', 'reviewed_text', 'review_source')
RUN_METADATA_KEYS = frozenset({'status', 'model', 'model_digest', 'request_id',
    'inference_requests_attempted', 'metrics', 'wall_seconds', 'limits',
    'memory_min_free_percent', 'model_vram_max_bytes', 'swap_used_mib_max',
    'admission_profile', 'controller_queue_visibility', 'swap_policy',
    'cleanup_confirmed', 'reason', 'error_stage', 'error_code',
    'request_attempted', 'attempt_count', 'error_http_status'})


class AppError(ValueError):
    """A safe local validation failure; never a reason for automatic retry."""


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _safe_metadata_value(value, *, nested=False):
    if value is None or type(value) is bool:
        return value
    if type(value) in (int, float):
        if abs(value) > 10**18 or not math.isfinite(value):
            raise AppError('Client metadata exceeds its numeric bound.')
        return value
    if type(value) is str:
        if len(value.encode('utf-8')) > 256 or any(ord(c) < 32 for c in value):
            raise AppError('Client metadata contains unsupported text.')
        return value
    if type(value) is dict and not nested and len(value) <= 32:
        if any(type(k) is not str or not re.fullmatch(r'[A-Za-z0-9_]{1,48}', k) for k in value):
            raise AppError('Client metadata contains unsupported keys.')
        return {k: _safe_metadata_value(v, nested=True) for k, v in value.items()}
    raise AppError('Client metadata contains an unsupported value.')


def _client_metadata(client):
    kind = getattr(client, 'kind', None)
    model = getattr(client, 'model_name', None)
    verified = getattr(client, 'execution_verified', False)
    last_run = getattr(client, 'last_run', {})
    if kind not in ('stub', 'granite') or type(model) is not str or not model or len(model) > 120:
        raise AppError('Use a supported explicitly identified local client.')
    if any(ord(c) < 32 for c in model) or type(verified) is not bool or type(last_run) is not dict:
        raise AppError('Client metadata is invalid.')
    # Only the known client telemetry fields cross into app persistence.
    safe = {k: _safe_metadata_value(v) for k, v in last_run.items() if k in RUN_METADATA_KEYS}
    if ('error_stage' in safe and safe['error_stage'] not in {
            'tag_lookup', 'digest_check', 'admission', 'resource_guard',
            'model_request', 'response_parse', 'selection_contract'}):
        raise AppError('Client diagnostic stage is invalid.')
    if ('error_code' in safe and safe['error_code'] not in {
            'MODEL_TAG_LOOKUP_FAILED', 'MODEL_TAG_UNAVAILABLE', 'MODEL_DIGEST_UNAVAILABLE',
            'MODEL_DIGEST_MISMATCH', 'ADMISSION_LOCK_FAILED', 'ADMISSION_CHECK_FAILED',
            'RESOURCE_ADMISSION_DENIED', 'SWAP_NONZERO', 'MODEL_REQUEST_FAILED', 'MODEL_HTTP_ERROR',
            'MODEL_RESPONSE_INVALID', 'SELECTION_CONTRACT_REJECTED', 'MODEL_SETUP_ERROR'}):
        raise AppError('Client diagnostic code is invalid.')
    if 'request_attempted' in safe and type(safe['request_attempted']) is not bool:
        raise AppError('Client diagnostic request flag is invalid.')
    if 'attempt_count' in safe and (type(safe['attempt_count']) is not int or safe['attempt_count'] not in (0, 1)):
        raise AppError('Client diagnostic request count is invalid.')
    if 'error_http_status' in safe and (type(safe['error_http_status']) is not int or not 100 <= safe['error_http_status'] <= 599):
        raise AppError('Client diagnostic HTTP status is invalid.')
    encoded = _json(safe)
    if len(encoded.encode('ascii')) > 8192:
        raise AppError('Client metadata exceeds its storage bound.')
    return kind, model, bool(kind == 'granite' and verified), encoded


def _content_digest(draft):
    # Bind the unique suggestion, original text, exact facts, revision and client
    # provenance. A regenerated identical sentence still requires a new review.
    keys = tuple(k for k in DRAFT_COLUMNS if k not in
                 ('content_digest', 'reviewed_at', 'reviewed_text', 'review_source'))
    return hashlib.sha256(_json({key: draft[key] for key in keys}).encode('ascii')).hexdigest()


class Store:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path).expanduser()
        self.conn = None
        if self.path.is_symlink():
            raise AppError('Refusing a linked database path; choose a dedicated simulation database.')
        if self.path.exists():
            self._check_existing()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            self._initialize()
        self.conn = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')

    def _check_existing(self):
        # No migration or writable connection to older/unrelated databases.
        try:
            uri = self.path.resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True)) as existing:
                identity = existing.execute('PRAGMA application_id').fetchone()[0]
                version = existing.execute('PRAGMA user_version').fetchone()[0]
                tables = {r[0] for r in existing.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if identity != APP_ID or version != DB_VERSION or tables != {'orders', 'suggestions'}:
                    raise AppError('Existing database is not this version 3 simulation database; no migration or change was made.')
                for table, expected in (('orders', ORDER_COLUMNS), ('suggestions', DRAFT_COLUMNS)):
                    if tuple(r[1] for r in existing.execute(f'PRAGMA table_info({table})')) != expected:
                        raise AppError('Existing version 3 database schema differs; it was not changed.')
        except sqlite3.Error as error:
            raise AppError('Existing file is not a readable simulation database; it was not changed.') from error

    def _initialize(self):
        with closing(sqlite3.connect(self.path)) as initial:
            with initial:
                initial.execute('BEGIN IMMEDIATE')
                initial.execute(f'PRAGMA application_id={APP_ID}')
                initial.execute(f'PRAGMA user_version={DB_VERSION}')
                initial.execute("""CREATE TABLE orders (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode='SIMULATED'),
                    source TEXT NOT NULL CHECK(source='SIMULATED_SANDBOX'), buyer TEXT NOT NULL,
                    seller_id TEXT NOT NULL, product_id TEXT NOT NULL, product_name TEXT NOT NULL,
                    currency TEXT NOT NULL, amount TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('CREATED','APPROVED','COMPLETED','VOIDED')),
                    capture_status TEXT CHECK(capture_status IS NULL OR capture_status IN ('PENDING','COMPLETED')),
                    revision INTEGER NOT NULL CHECK(revision>=0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    CHECK(status='COMPLETED' OR capture_status IS NULL))""")
                initial.execute("""CREATE TABLE suggestions (
                    order_id TEXT PRIMARY KEY REFERENCES orders(id), suggestion_id TEXT NOT NULL UNIQUE,
                    order_revision INTEGER NOT NULL, category TEXT NOT NULL,
                    phrase_id TEXT NOT NULL, style_id TEXT NOT NULL, facts_fingerprint TEXT NOT NULL,
                    reply_text TEXT NOT NULL, approved_body TEXT NOT NULL, content_digest TEXT NOT NULL,
                    client_kind TEXT NOT NULL CHECK(client_kind IN ('stub','granite')), model_name TEXT NOT NULL,
                    model_execution_verified INTEGER NOT NULL CHECK(model_execution_verified IN (0,1)),
                    client_metadata TEXT NOT NULL, created_at TEXT NOT NULL,
                    reviewed_at TEXT, reviewed_text TEXT,
                    review_source TEXT CHECK(review_source IS NULL OR review_source='local_operator_explicit'),
                    CHECK((reviewed_at IS NULL AND reviewed_text IS NULL AND review_source IS NULL)
                       OR (reviewed_at IS NOT NULL AND reviewed_text IS NOT NULL AND review_source IS NOT NULL)))""")

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    @contextmanager
    def _transaction(self, *, immediate=False):
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute('BEGIN IMMEDIATE' if immediate else 'BEGIN')
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    @staticmethod
    def _facts(order):
        try:
            return OrderFacts(order_id=order['id'], status=order['status'],
                capture_status=order['capture_status'], seller_id=order['seller_id'],
                product_id=order['product_id'], product_name=order['product_name'],
                currency=order['currency'], amount=order['amount'], source=order['source'])
        except (TypeError, ValueError, KeyError) as error:
            raise AppError('Stored order facts are outside this fixed simulation scope.') from error

    def _read_order(self, order_id):
        row = self.conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        if row is None:
            raise AppError('Synthetic order not found.')
        result = dict(row)
        self._facts(result)
        draft = self.conn.execute('SELECT * FROM suggestions WHERE order_id=?', (order_id,)).fetchone()
        result['draft'] = dict(draft) if draft else None
        if result['draft']:
            try:
                result['draft']['client_metadata'] = json.loads(result['draft']['client_metadata'])
            except (TypeError, ValueError) as error:
                raise AppError('Stored suggestion metadata is invalid; generate a fresh suggestion.') from error
            result['draft']['model_execution_verified'] = bool(result['draft']['model_execution_verified'])
        return result

    def get_order(self, order_id):
        with self._transaction():
            return self._read_order(order_id)

    def list_orders(self):
        with self._transaction():
            rows = [dict(row) for row in self.conn.execute('SELECT * FROM orders ORDER BY created_at, id')]
            for row in rows:
                self._facts(row)
            return rows

    def create_order(self):
        identifier = 'SIM-' + uuid4().hex[:12].upper()
        now = timestamp()
        with self._transaction(immediate=True):
            self.conn.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier, 'SIMULATED', 'SIMULATED_SANDBOX', 'Sample Buyer', SELLER_ID,
                 PRODUCT_ID, PRODUCT_NAME, CURRENCY, AMOUNT, 'CREATED', None, 0, now, now))
        return self.get_order(identifier)

    def set_status(self, order_id, status):
        if type(status) is not str or status not in STATUSES:
            raise AppError('Use an exact mock status: ' + ', '.join(STATUSES))
        with self._transaction(immediate=True):
            order = self._read_order(order_id)
            if status != order['status']:
                if status not in NEXT[order['status']]:
                    raise AppError('Unsupported mock transition; create a new synthetic order to start again.')
                capture = 'COMPLETED' if status == 'COMPLETED' else None
                self.conn.execute('UPDATE orders SET status=?, capture_status=?, revision=revision+1, updated_at=? WHERE id=?',
                                  (status, capture, timestamp(), order_id))
                self.conn.execute('DELETE FROM suggestions WHERE order_id=?', (order_id,))
        return self.get_order(order_id)

    def draft_reply(self, order_id, client):
        # Model I/O occurs after the read transaction closes. Never hold the
        # SQLite write lock while waiting for a local model or injected client.
        before = self.get_order(order_id)
        facts = self._facts(before)
        original = fingerprint_order_facts(facts)
        try:
            suggestion = generate_suggestion(facts, client)
        except SuggestionError as error:
            raise AppError('Suggestion generation held or failed; no draft saved and no automatic retry.') from error
        kind, model, verified, metadata = _client_metadata(client)
        draft = {'order_id': order_id, 'suggestion_id': 'SUG-' + uuid4().hex.upper(),
                 'order_revision': before['revision'], 'category': suggestion.category,
                 'phrase_id': suggestion.phrase_id, 'style_id': suggestion.style_id,
                 'facts_fingerprint': suggestion.facts_fingerprint, 'reply_text': suggestion.reply_text,
                 'approved_body': suggestion._approved_body, 'client_kind': kind, 'model_name': model,
                 'model_execution_verified': int(verified), 'client_metadata': metadata,
                 'created_at': timestamp(), 'reviewed_at': None, 'reviewed_text': None, 'review_source': None}
        draft['content_digest'] = _content_digest(draft)
        with self._transaction(immediate=True):
            current = self._read_order(order_id)
            if current['revision'] != before['revision'] or fingerprint_order_facts(self._facts(current)) != original:
                raise AppError('Order changed during suggestion generation; discard this result and request a fresh draft.')
            placeholders = ','.join('?' for _ in DRAFT_COLUMNS)
            self.conn.execute(f'INSERT OR REPLACE INTO suggestions VALUES ({placeholders})',
                              tuple(draft[column] for column in DRAFT_COLUMNS))
        return self.get_order(order_id)

    def review_draft(self, order_id, *, approve=False, expected_digest=None):
        if approve is not True:
            raise AppError('Explicit human --approve is required; this action never sends a message.')
        if type(expected_digest) is not str or not re.fullmatch('[0-9a-f]{64}', expected_digest):
            raise AppError('Review requires the exact suggestion digest shown with the draft.')
        with self._transaction(immediate=True):
            order = self._read_order(order_id)
            draft = order['draft']
            if not draft or draft['order_revision'] != order['revision']:
                raise AppError('Generate and inspect a fresh suggestion for the current order revision.')
            # Restore the exact storage representation used by the digest.
            stored = dict(draft)
            stored['client_metadata'] = _json(stored['client_metadata'])
            stored['model_execution_verified'] = int(stored['model_execution_verified'])
            if draft['content_digest'] != expected_digest or _content_digest(stored) != expected_digest:
                raise AppError('Suggestion changed; inspect a fresh draft before recording review.')
            suggestion = ReplySuggestion(order_id=draft['order_id'], category=draft['category'],
                phrase_id=draft['phrase_id'], style_id=draft['style_id'],
                facts_fingerprint=draft['facts_fingerprint'], reply_text=draft['reply_text'],
                _approved_body=draft['approved_body'])
            try:
                approved = suggestion.approve(self._facts(order))
            except SuggestionError as error:
                raise AppError('Suggestion facts or content changed; generate and inspect a fresh draft.') from error
            if draft['reviewed_at'] is not None:
                if draft['reviewed_text'] != approved.reply_text or draft['review_source'] != 'local_operator_explicit':
                    raise AppError('Stored review content changed; generate and inspect a fresh draft.')
                return order
            self.conn.execute('UPDATE suggestions SET reviewed_at=?, reviewed_text=?, review_source=? WHERE order_id=?',
                              (timestamp(), approved.reply_text, 'local_operator_explicit', order_id))
        return self.get_order(order_id)


def say(message, *, file=None):
    for line in str(message).splitlines() or ['']:
        print('[SIMULATED] ' + line, file=file or sys.stdout)


def sandbox_order_view(*, refresh=False, review=False):
    """Display one receipt-pinned sandbox order; never mutate or draft for it."""
    from sandbox_reconciliation import read_recorded_evidence, refresh_known_order
    facts = refresh_known_order() if refresh else read_recorded_evidence()
    print('[PAYPAL SANDBOX READ-ONLY] ' + facts['order_id'])
    print('Source: ' + facts['source'])
    if facts.get('observed_at'):
        label = 'Live read observed at: ' if facts.get('source') == 'live_paypal_sandbox_read' else 'Example timestamp: ' if facts.get('source') == 'synthetic_order_fixture' else 'Receipt observed at: '
        print(label + facts['observed_at'])
    print('Order status: ' + facts['order_status'])
    print('Amount: ' + facts['currency'] + ' ' + facts['amount'])
    print('Payment truth: ' + facts['payment_truth'])
    print('Capture status: ' + str(facts['capture_status']))
    if review:
        print('This command displays facts only; it does not generate a draft.')
        if facts['payment_truth'] == 'CREATED_NOT_APPROVED':
            print('A draft must state that buyer approval and captured payment are not established.')
        print('For a separate offline draft using synthetic example data, run:')
        print('python3 -B app.py --client stub sandbox-ai-review')
        print('That preview uses fixed wording, makes no PayPal or model request, and leaves review pending.')
        print('The sandbox view is read-only. It has no approve, capture, refund, fulfillment, or send action.')
    if refresh:
        print('Refresh made one read-only GET after OAuth. No create, approve, capture, refund, or send action exists here.')
    else:
        print('This is synthetic example data, not a live refresh or PayPal lookup.' if facts['source'] == 'synthetic_order_fixture' else 'This is saved receipt evidence, not a live refresh. Use --refresh for one read-only sandbox lookup.')


class SimulationParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        if message:
            say(message.rstrip('\n'), file=file or sys.stderr)


def show(order, *, db=None):
    say(f"{order['id']} | {order['product_name']} | synthetic buyer: {order['buyer']}")
    say(f"Source: {order['source']} | seller: {order['seller_id']} | mock revision: {order['revision']}")
    say('Mock order status: ' + order['status'] + ' | mock capture: ' + str(order['capture_status']))
    say('APPROVED never means paid. Only a matching COMPLETED mock capture supports the simulated completed-payment wording.')
    draft = order['draft']
    if draft:
        say('Reply category: ' + draft['category'])
        say('Wording selection: ' + draft['client_kind'] + ' / ' + draft['model_name'])
        say('Actual local model execution verified by client: ' + str(draft['model_execution_verified']))
        say(draft['reviewed_text'] if draft['reviewed_at'] else draft['reply_text'])
        say('Local human review: ' + (draft['reviewed_at'] or 'PENDING; no automatic approval'))
        say('Suggestion digest: ' + draft['content_digest'])
        if db is not None and not draft['reviewed_at']:
            import shlex
            say('After inspecting this exact draft, an operator may record local review:')
            say('python3 app.py --db ' + shlex.quote(str(db)) + ' review ' + order['id'] +
                ' --approve --suggestion-digest ' + draft['content_digest'])
    else:
        say('No current suggestion. Status changes invalidate earlier drafts and reviews.')
    say('Delivery: NEVER SENT. This synthetic flow has no outbound-message or real-payment operation.')


def _make_client(name, *, standalone_demo=False, stable_swap_policy=None):
    try:
        from local_model import GraniteClient, StubClient
        return (GraniteClient(standalone_demo=standalone_demo,
                              stable_swap_policy=stable_swap_policy)
                if name == 'granite' else StubClient())
    except Exception as error:
        if getattr(error, 'failure_code', None) == 'MODEL_PLATFORM_UNSUPPORTED':
            raise AppError('Real Granite is unsupported on this platform; use --client stub for the offline demo. No live request was made.') from None
        raise AppError('Local client is unavailable or held; no automatic retry.') from None


def _confirm_standalone_demo(client):
    """Operator confirms isolation because portable mode cannot inspect task queues."""
    if not getattr(client, 'standalone_demo', False):
        return True
    from runtime_guard import swap_policy
    policy = swap_policy(standalone_demo=True)
    swap_limit = ('zero swap' if policy['max_swap_mib'] == 0
                  else f"at most {policy['max_swap_mib']} MiB allocated swap")
    print('LOCAL RESPONSE SETUP: Resource checks cannot detect all scheduled or background work.')
    print('Continue only after confirming this computer is free of other local model or heavy processing work.')
    print(f'Checks require the verified local model, no model already loaded, >=40% memory headroom, {swap_limit}, and sufficient disk space.')
    try:
        answer = input('Type RUN to confirm this computer is idle and allow one local response attempt: ')
    except (EOFError, KeyboardInterrupt):
        answer = ''
    if not client.attest_standalone(answer):
        print('Standalone inference not started; no model request or external action occurred.')
        return False
    try:
        checked = client.preflight()
    except Exception:
        print('Standalone preflight held; no model request or external action occurred.')
        return False
    resources = checked['resources']
    print('Standalone preflight passed: ' + checked['cached_models'][0] +
          ' digest ' + checked['model_digests'][checked['cached_models'][0]][:12] +
          '…; ' + str(resources['memory_pressure_free_percent']) +
          '% memory headroom; memory checks passed; no other model loaded.')
    print('Your confirmation expires after 10 minutes. This session permits one response attempt.')
    return True


def main(argv=None, *, client=None):
    parser = SimulationParser(description=__doc__,
        epilog='Local create/draft/review commands are SIMULATED ONLY. Stub is the default. The separate known-order sandbox view is read-only.')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='dedicated version 3 simulation SQLite file')
    parser.add_argument('--client', choices=('stub', 'granite'), default='stub',
                        help='wording selector; granite explicitly requests guarded local inference')
    parser.add_argument('--portable-standalone', action='store_true',
                        help='isolated macOS/Linux mode with local resource checks and explicit confirmation before one response attempt')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('create', help='create one fixed synthetic seller/product order')
    commands.add_parser('list', help='list synthetic orders')
    commands.add_parser('demo', help='walk mock statuses, then generate exactly one pending suggestion')
    sandbox = commands.add_parser('sandbox-order', help='show the one known PayPal sandbox order (read-only)')
    sandbox.add_argument('--refresh', action='store_true',
                         help='authenticate using the owner-only sandbox file and GET only the receipt-pinned order')
    sandbox_review = commands.add_parser('sandbox-review', help='read-only human-review context for the one known sandbox order')
    sandbox_review.add_argument('--refresh', action='store_true',
                         help='authenticate using the owner-only sandbox file and GET only the receipt-pinned order')
    ai_review = commands.add_parser('sandbox-ai-review', help='generate a receipt-grounded local AI review draft and request explicit in-session human review')
    ai_review.add_argument('--saved-reviewer-order', action='store_true',
                           help='use a validated historical Sandbox pin/journal instead of the synthetic fixture; no PayPal request')
    ai_review.add_argument('--reviewer-order-pin', type=Path,
                           help='existing owner-only pin for the saved Sandbox receipt')
    ai_review.add_argument('--reviewer-order-journal', type=Path,
                           help='existing successful create/read journal matching the pin')
    review_ui = commands.add_parser('sandbox-review-ui', help='open a private loopback browser page for the saved sandbox review flow')
    review_ui.add_argument('--port', type=int, default=8765,
                           help='loopback port (0 selects an available temporary port)')
    review_ui.add_argument('--refresh-known-order', action='store_true',
                           help='enable one explicit read-only refresh of the existing receipt-pinned sandbox order; requires --client granite')
    review_ui.add_argument('--refresh-reviewer-order', action='store_true',
                           help='enable one read-only GET of the owner-configured Sandbox order; requires the hidden-input order-pin helper; works with stub or Granite client')
    review_ui.add_argument('--reviewer-order-pin', type=Path, default=None,
                           help='owner-only pin file to bind the reviewer refresh and AI fact validation to the same order')
    review_ui.add_argument('--reviewer-order-journal', type=Path, default=None,
                           help='explicit owner-only creation journal paired with the selected pin')
    review_ui.add_argument('--judge-order-verification', action='store_true',
                           help='require the canonical dedicated judge pin to match its owner-only successful create/read journal before opening the UI; local-only check')
    review_ui.add_argument('--stable-swap-baseline', action='store_true',
                           help='for one separately approved judge run only: permit a measured stable pre-existing swap baseline; every other resource gate remains enforced')
    for name in ('show', 'status', 'draft', 'review'):
        sub = commands.add_parser(name, help='SIMULATED ' + name)
        sub.add_argument('order_id')
        if name == 'status':
            sub.add_argument('new_status', choices=STATUSES)
        if name == 'review':
            sub.add_argument('--approve', action='store_true', required=True, help='explicit local human review only')
            sub.add_argument('--suggestion-digest', required=True, help='digest printed with the exact inspected draft')
    args = parser.parse_args(argv)
    try:
        if args.portable_standalone and (args.client != 'granite'
                or args.command not in ('sandbox-ai-review', 'sandbox-review-ui')):
            raise AppError('--portable-standalone requires --client granite and a sandbox AI review command.')
        if args.command == 'sandbox-review-ui' and args.refresh_known_order and args.client != 'granite':
            raise AppError('--refresh-known-order requires --client granite; offline stub mode remains credential-free.')
        if args.command == 'sandbox-review-ui' and args.refresh_known_order and args.refresh_reviewer_order:
            raise AppError('Choose one Sandbox order source for this session.')
        if args.command == 'sandbox-review-ui' and args.reviewer_order_pin is not None and not args.refresh_reviewer_order:
            raise AppError('--reviewer-order-pin requires --refresh-reviewer-order.')
        if args.command == 'sandbox-review-ui' and args.refresh_known_order and args.reviewer_order_pin is not None:
            raise AppError('--reviewer-order-pin cannot be used with --refresh-known-order.')
        if args.command == 'sandbox-review-ui' and args.refresh_known_order and args.portable_standalone:
            raise AppError('--refresh-known-order is unavailable in portable standalone mode; use the controller-aware Mac guard.')
        if getattr(args, 'judge_order_verification', False) and (args.command != 'sandbox-review-ui'
                or args.client != 'granite' or not args.refresh_reviewer_order
                or args.reviewer_order_pin is None or args.portable_standalone):
            raise AppError('--judge-order-verification requires Granite, --refresh-reviewer-order, an explicit --reviewer-order-pin, and controller-aware admission.')
        if getattr(args, 'stable_swap_baseline', False) and not getattr(args, 'judge_order_verification', False):
            raise AppError('--stable-swap-baseline requires --judge-order-verification.')
        if args.command in ('sandbox-order', 'sandbox-review'):
            from sandbox_adapter import AdapterError
            try:
                sandbox_order_view(refresh=args.refresh, review=args.command == 'sandbox-review')
                return 0
            except AdapterError as error:
                stage = error.stage if error.stage in ('validation', 'oauth_transport', 'oauth_http',
                                                        'oauth_response', 'transport', 'response') else 'reconciliation'
                code = error.failure_code if re.fullmatch(r'[A-Z0-9_]{1,64}', error.failure_code or '') else 'SAFE_FAILURE'
                print(f'[PAYPAL SANDBOX READ-ONLY] unavailable at {stage} ({code}); no automatic retry.', file=sys.stderr)
                return 2
        if args.command == 'sandbox-ai-review':
            from sandbox_reconciliation import read_recorded_evidence, read_saved_judge_order_facts
            from sandbox_adapter import AdapterError
            from sandbox_ai_review import (LABEL, facts_from_receipt,
                generate_sandbox_review, record_human_review)
            if args.saved_reviewer_order:
                if (args.reviewer_order_pin is None or args.reviewer_order_journal is None
                        or not args.reviewer_order_pin.is_absolute()
                        or not args.reviewer_order_journal.is_absolute()):
                    raise AppError('Saved review requires explicit absolute pin and journal paths.')
                try:
                    saved = read_saved_judge_order_facts(pin_path=args.reviewer_order_pin,
                                                       journal_path=args.reviewer_order_journal)
                    facts = facts_from_receipt(saved, pin_path=args.reviewer_order_pin)
                except (AdapterError, ValueError):
                    raise AppError('Saved Sandbox pin/journal failed validation; no model or PayPal request was made.') from None
            else:
                if args.reviewer_order_pin is not None or args.reviewer_order_journal is not None:
                    raise AppError('Pin and journal options require --saved-reviewer-order.')
                facts = facts_from_receipt(read_recorded_evidence())
            selected = client if client is not None else _make_client(args.client,
                standalone_demo=args.portable_standalone)
            if not _confirm_standalone_demo(selected):
                return 2
            suggestion = generate_sandbox_review(facts, selected)
            print('PAYPAL SANDBOX — synthetic example data; no API request made.' if facts.source == 'synthetic_order_fixture' else 'PAYPAL SANDBOX — saved receipt evidence; no API request made.')
            print('Order status: CREATED | Payment truth: CREATED_NOT_APPROVED | Capture: none')
            print('Evidence source: ' + facts.source + ' | observed: ' + facts.observed_at)
            print('AI selection: ' + str(getattr(selected, 'kind', 'injected')) + ' / ' +
                  str(getattr(selected, 'model_name', 'unknown')) + ' | execution verified: ' +
                  str(bool(getattr(selected, 'execution_verified', False))))
            if getattr(selected, 'kind', None) == 'granite' and getattr(selected, 'execution_verified', False):
                print(LABEL)
            else:
                print('OFFLINE REVIEW PREVIEW — NO MODEL INFERENCE; NOT SENT')
            print(suggestion.review_text)
            print('Seller next step: ' + suggestion.seller_next_step)
            print('Review digest: ' + suggestion.review_digest)
            print('HUMAN REVIEW PENDING. This draft is not sent; this command has no PayPal action.')
            try:
                answer = input('Type REVIEW to record that you inspected this exact draft in this session: ')
            except (EOFError, KeyboardInterrupt):
                answer = ''
            if answer != 'REVIEW':
                print('Review remains pending; nothing was sent or changed at PayPal.')
                return 0
            result = record_human_review(suggestion, facts, suggestion.review_digest)
            print('Local human review recorded for this session: ' + result.status +
                  ' | external action: none | payment truth: ' + result.payment_truth)
            print('This review acknowledgment is not persisted between runs.')
            return 0
        if args.command == 'sandbox-review-ui':
            from sandbox_review_ui import serve_review_ui
            if args.judge_order_verification:
                from reviewer_sandbox_order import validate_judge_order_binding
                from sandbox_adapter import AdapterError
                try:
                    validate_judge_order_binding(pin_path=args.reviewer_order_pin, journal_path=args.reviewer_order_journal)
                except AdapterError as error:
                    code = (error.failure_code if isinstance(error.failure_code, str)
                            and re.fullmatch(r'[A-Z0-9_]{1,64}', error.failure_code)
                            else 'JUDGE_BINDING_INVALID')
                    raise AppError(f'Judge pin/journal check failed ({code}); no PayPal request was made.') from None
                print('Local judge pin and successful create/read journal match. No PayPal request has been made.')
            stable_swap_policy = None
            if args.stable_swap_baseline:
                from runtime_guard import StableSwapBaseline
                stable_swap_policy = StableSwapBaseline()
            selected = _make_client(args.client, standalone_demo=args.portable_standalone,
                                    stable_swap_policy=stable_swap_policy)
            if not _confirm_standalone_demo(selected):
                return 2
            refresh = None
            if args.refresh_known_order:
                from sandbox_reconciliation import refresh_known_order
                refresh = refresh_known_order
            elif args.refresh_reviewer_order:
                from sandbox_reconciliation import refresh_reviewer_order
                refresh = (partial(refresh_reviewer_order, pin_path=args.reviewer_order_pin)
                           if args.reviewer_order_pin is not None else refresh_reviewer_order)
            serve_review_ui(selected, port=args.port, refresh_known_order=refresh,
                            reviewer_pin_path=args.reviewer_order_pin)
            return 0
        with closing(Store(args.db)) as store:
            if args.command == 'create':
                show(store.create_order(), db=args.db)
            elif args.command == 'list':
                orders = store.list_orders()
                if not orders:
                    say('No synthetic orders yet. Run create or demo.')
                for order in orders:
                    say(f"{order['id']} | {order['status']} | {order['product_name']} | {order['source']}")
            elif args.command == 'show':
                show(store.get_order(args.order_id), db=args.db)
            elif args.command == 'status':
                show(store.set_status(args.order_id, args.new_status), db=args.db)
            elif args.command == 'review':
                show(store.review_draft(args.order_id, approve=args.approve,
                                       expected_digest=args.suggestion_digest), db=args.db)
            else:
                selected = client if client is not None else _make_client(args.client)
                if args.command == 'demo':
                    order = store.create_order()
                    for status in ('APPROVED', 'COMPLETED'):
                        order = store.set_status(order['id'], status)
                        say('Mock transition: ' + status)
                    identifier = order['id']
                else:
                    identifier = args.order_id
                show(store.draft_reply(identifier, selected), db=args.db)
                say('Suggestion remains pending until a separate explicit human review action.')
    except AppError as error:
        say('Error: ' + str(error))
        return 2
    except (OSError, sqlite3.Error):
        say('Local storage operation failed; no automatic retry or external fallback.')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
