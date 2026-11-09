"""One-shot judge-owned PayPal Sandbox create → read → local draft flow.

This command is inert unless run interactively with --run and the exact order
creation confirmation. It only creates a synthetic USD 10 Sandbox order, reads
that returned order once, and asks the selected local client for a private
draft. There is no approve, capture, refund, payment, or send operation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
from uuid import uuid4

from private_file_support import supports_private_files
from facts import CURRENCY, AMOUNT
from reviewer_sandbox_order import DEFAULT_JUDGE_ORDER_PIN, write_order_pin
from sandbox_adapter import (AdapterError, HttpRequest, HttpResponse,
                             SandboxAdapter, SandboxOAuthClient,
                             UrllibSandboxTransport,
                             load_owner_sandbox_credentials,
                             _safe_transport_exception)

DEFAULT_STATE = Path.home() / '.secondbrain-secrets' / 'paypal-judge-flow-once.json'
CONFIRMATION = 'CREATE ONE USD 10 SANDBOX ORDER'
SAFE_STAGE = re.compile(r'[a-z][a-z0-9_]{0,31}\Z')
SAFE_CODE = re.compile(r'[A-Z][A-Z0-9_]{0,63}\Z')
SAFE_EXCEPTION = frozenset({'TimeoutError', 'gaierror', 'SSLError',
    'ConnectionRefusedError', 'ConnectionResetError', 'OSError', 'RuntimeError', 'Exception'})
SAFE_ERROR_NAME = re.compile(r'[A-Z][A-Z0-9_]{0,63}\Z')
SAFE_OAUTH_ERROR = re.compile(r'[a-z][a-z0-9_]{0,31}\Z')
SAFE_DEBUG_ID = re.compile(r'[A-Za-z0-9-]{1,128}\Z')


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _safe_error_name(body: bytes) -> str | None:
    if type(body) is not bytes or len(body) > 32768:
        return None
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if type(value) is not dict:
        return None
    name = value.get('name')
    if type(name) is str and SAFE_ERROR_NAME.fullmatch(name):
        return name
    # PayPal OAuth commonly returns a lowercase `error` code such as
    # `invalid_client`; retain only this bounded identifier, never description.
    oauth_error = value.get('error')
    return (oauth_error if type(oauth_error) is str
            and SAFE_OAUTH_ERROR.fullmatch(oauth_error) else None)


def _classify_request(request: HttpRequest):
    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(request.url)
    except Exception:
        return None
    if parsed.scheme != 'https' or parsed.netloc != 'api-m.sandbox.paypal.com':
        return None
    if request.method == 'POST' and parsed.path == '/v1/oauth2/token':
        return 'oauth'
    if request.method == 'POST' and parsed.path == '/v2/checkout/orders':
        return 'order_create'
    if (request.method == 'GET'
            and re.fullmatch(r'/v2/checkout/orders/[A-Z0-9]{12,24}', parsed.path)):
        return 'order_get'
    return None


class OneShotJournal:
    """Private, atomic attempt record. It intentionally contains no raw HTTP data."""
    def __init__(self, path: Path, *, request_id: str, now=utc_now):
        self.path = Path(path)
        self.now = now
        self.value = {
            'schema': 'judge-sandbox-create-read-draft-v1',
            'state': 'prepared',
            'environment': 'PayPal Sandbox only',
            'createdAt': now(),
            'updatedAt': now(),
            'idempotencyKey': request_id,
            'orderId': None,
            'configurationId': None,
            'calls': [],
            'model': {'attempted': False, 'status': 'not_started',
                      'stage': None, 'code': None, 'requestCount': 0},
        }

    def _check_parent(self):
        if not supports_private_files():
            raise AdapterError('Private Sandbox files require supported POSIX ownership and permissions',
                               stage='journal', failure_code='PRIVATE_FILES_UNSUPPORTED')
        parent = self.path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
                or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077):
            raise AdapterError('Judge flow directory is not owner-only', stage='journal',
                               failure_code='JOURNAL_DIRECTORY_INVALID')

    def create(self):
        self._check_parent()
        encoded = (json.dumps(self.value, sort_keys=True, indent=2) + '\n').encode()
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            raise AdapterError('A prior one-shot attempt already exists; no retry is allowed',
                               stage='journal', failure_code='ONE_SHOT_ALREADY_USED') from None
        except OSError:
            raise AdapterError('Private one-shot journal could not be created',
                               stage='journal', failure_code='JOURNAL_CREATE_FAILED') from None
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb', closefd=True) as stream:
                fd = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if fd >= 0:
                os.close(fd)

    def save(self):
        self._check_parent()
        try:
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise AdapterError('Private one-shot journal permissions changed', stage='journal',
                                   failure_code='JOURNAL_PERMISSIONS_INVALID')
            self.value['updatedAt'] = self.now()
            encoded = (json.dumps(self.value, sort_keys=True, indent=2) + '\n').encode()
            tmp = self.path.with_name(self.path.name + '.' + uuid4().hex + '.tmp')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                with os.fdopen(fd, 'wb', closefd=True) as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, self.path)
                os.chmod(self.path, 0o600)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
        except AdapterError:
            raise
        except OSError:
            raise AdapterError('Private one-shot journal could not be updated', stage='journal',
                               failure_code='JOURNAL_UPDATE_FAILED') from None


class DiagnosticTransport:
    """Allowlist three request shapes and retain only bounded diagnostics."""
    LIMITS = {'oauth': 1, 'order_create': 1, 'order_get': 1}

    def __init__(self, transport, journal: OneShotJournal):
        self.transport = transport
        self.journal = journal

    def request(self, request: HttpRequest) -> HttpResponse:
        stage = _classify_request(request)
        if stage is None:
            raise AdapterError('Request is outside the one-shot judge flow', stage='transport',
                               failure_code='REQUEST_OUTSIDE_ALLOWLIST')
        counts = {key: sum(event['stage'] == key for event in self.journal.value['calls'])
                  for key in self.LIMITS}
        if counts[stage] >= self.LIMITS[stage]:
            raise AdapterError('One-shot request limit reached; no retry', stage=stage,
                               failure_code='ONE_SHOT_LIMIT_REACHED')
        if stage == 'order_create':
            if counts['oauth'] != 1 or self.journal.value['calls'][-1]['stage'] != 'oauth':
                raise AdapterError('Order create must follow one successful OAuth exchange',
                                   stage='order_create', failure_code='REQUEST_SEQUENCE_INVALID')
            if request.headers.get('PayPal-Request-Id') != self.journal.value['idempotencyKey']:
                raise AdapterError('Order create idempotency key did not match private journal',
                                   stage='order_create', failure_code='IDEMPOTENCY_KEY_MISMATCH')
        if stage == 'order_get':
            if counts['order_create'] != 1 or not self.journal.value.get('orderId'):
                raise AdapterError('Order read must follow the one recorded create',
                                   stage='order_get', failure_code='REQUEST_SEQUENCE_INVALID')
            order_id = request.url.rsplit('/', 1)[-1]
            if order_id != self.journal.value['orderId']:
                raise AdapterError('Order read must use the ID returned by create',
                                   stage='order_get', failure_code='ORDER_ID_MISMATCH')
        event = {'stage': stage, 'method': request.method,
                 'requestAt': self.journal.now(), 'outcome': 'pending',
                 'httpStatus': None, 'debugId': None, 'errorName': None,
                 'failureCode': None, 'exceptionClass': None, 'responseAt': None}
        self.journal.value['calls'].append(event)
        self.journal.save()  # crash-safe: an uncertain request is never replayed
        try:
            response = self.transport.request(request)
        except AdapterError as error:
            event.update(outcome='transport_error',
                         failureCode=(error.failure_code if type(error.failure_code) is str
                                      and SAFE_CODE.fullmatch(error.failure_code) else 'TRANSPORT_FAILURE'),
                         exceptionClass=(error.underlying_exception_class
                                         if error.underlying_exception_class in SAFE_EXCEPTION else None),
                         httpStatus=error.http_status)
            event['responseAt'] = self.journal.now()
            self.journal.save()
            raise
        except Exception as error:
            exception_class, failure_code = _safe_transport_exception(error)
            event.update(outcome='transport_error', failureCode='TRANSPORT_FAILURE',
                         exceptionClass=(exception_class if exception_class in SAFE_EXCEPTION else 'Exception'),
                         responseAt=self.journal.now())
            event['failureCode'] = (failure_code if SAFE_CODE.fullmatch(failure_code)
                                    else 'TRANSPORT_FAILURE')
            self.journal.save()
            raise AdapterError('Sandbox transport failed; no retry', stage=stage,
                               failure_code='SANDBOX_TRANSPORT_FAILURE',
                               underlying_exception_class=event['exceptionClass']) from None
        if not isinstance(response, HttpResponse):
            event.update(outcome='malformed_response', failureCode='MALFORMED_HTTP_RESPONSE',
                         responseAt=self.journal.now())
            self.journal.save()
            raise AdapterError('Malformed HTTP response; no retry', stage=stage,
                               failure_code='MALFORMED_HTTP_RESPONSE')
        event.update(outcome='response',
                     httpStatus=(response.status_code if type(response.status_code) is int
                                 and 100 <= response.status_code <= 599 else None),
                     debugId=(response.debug_id if type(response.debug_id) is str
                              and SAFE_DEBUG_ID.fullmatch(response.debug_id) else None),
                     errorName=(_safe_error_name(response.body)
                                if type(response.status_code) is int and response.status_code >= 400 else None),
                     responseAt=self.journal.now())
        self.journal.save()
        return response


def _safe_exception(error):
    stage = getattr(error, 'stage', None)
    code = getattr(error, 'failure_code', None)
    return {
        'stage': stage if type(stage) is str and SAFE_STAGE.fullmatch(stage) else 'flow',
        'code': code if type(code) is str and SAFE_CODE.fullmatch(code) else 'FLOW_STOPPED',
        'httpStatus': getattr(error, 'http_status', None)
            if type(getattr(error, 'http_status', None)) is int else None,
    }


def atomic_new_order_pin(path, order_id):
    """Publish a fully fsynced pin without replacing any prior private pin."""
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        pin = write_order_pin(temporary, order_id)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            raise AdapterError('Order pin already exists; preserve both prior state and journal',
                stage='configuration', failure_code='ORDER_PIN_EXISTS') from None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return pin
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def run_once(*, transport, credentials, granite_client, question: str,
             confirm_create, journal_path: Path = DEFAULT_STATE,
             pin_path: Path = DEFAULT_JUDGE_ORDER_PIN, now=utc_now,
             request_id_factory=lambda: str(uuid4()), pin_writer=atomic_new_order_pin):
    """Execute at most OAuth→one POST→one immediate GET→one local draft.

    A successful caller must obtain a separate operator confirmation before
    calling this function. All network traffic is injected for tests; the CLI
    supplies the fixed HTTPS sandbox transport and owner-only credentials.
    """
    from sandbox_ai_review import (SandboxOrderFacts, generate_sandbox_review,
                                   normalize_payment_question)
    if not supports_private_files():
        raise AdapterError('Private Sandbox files require supported POSIX ownership and permissions',
                           stage='journal', failure_code='PRIVATE_FILES_UNSUPPORTED')
    question = normalize_payment_question(question)
    if Path(pin_path).exists() or Path(pin_path).is_symlink():
        raise AdapterError('An existing Sandbox order pin is present; no order was created',
                           stage='configuration', failure_code='ORDER_PIN_ALREADY_EXISTS')
    request_id = request_id_factory()
    if type(request_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id):
        raise AdapterError('Could not create a bounded idempotency key', stage='configuration',
                           failure_code='IDEMPOTENCY_KEY_INVALID')
    if not confirm_create():
        return {'state': 'cancelled', 'calls': [], 'draft': None}
    credentials = credentials() if callable(credentials) else credentials
    journal = OneShotJournal(Path(journal_path), request_id=request_id, now=now)
    journal.create()
    journal.value['state'] = 'starting_oauth'
    journal.save()
    diagnostic_transport = DiagnosticTransport(transport, journal)
    token = None
    try:
        token = SandboxOAuthClient(diagnostic_transport, credentials, enabled=True).fetch_token()
        journal.value['state'] = 'oauth_succeeded'
        journal.save()
        adapter = SandboxAdapter(diagnostic_transport, token)
        created = adapter.create_first_party_test_order(request_id)
        journal.value.update(state='created_waiting_for_immediate_read', orderId=created['order_id'],
                             createdOrderStatus=created['order_status'])
        journal.save()
        readback = adapter.get_known_sandbox_order(created['order_id'])
        observed_at = now()
        try:
            observed_at = datetime.fromisoformat(observed_at.replace('Z', '+00:00')).astimezone(
                timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        except (AttributeError, TypeError, ValueError, OverflowError):
            journal.value.update(state='readback_rejected', failure={
                'stage': 'order_validation', 'code': 'OBSERVATION_TIME_INVALID'})
            journal.save()
            raise AdapterError('Sandbox observation time failed validation; no draft generated',
                               stage='order_validation', failure_code='OBSERVATION_TIME_INVALID') from None
        if (created['order_status'] != 'CREATED'
                or readback.order_status != 'CREATED'
                or readback.payment_truth != 'CREATED_NOT_APPROVED'
                or readback.capture_status is not None
                or readback.currency != CURRENCY or readback.amount != AMOUNT):
            journal.value.update(state='readback_rejected', readbackAt=observed_at,
                                 failure={'stage': 'order_validation', 'code': 'ORDER_NOT_REVIEWABLE'})
            journal.save()
            return {'state': 'readback_rejected', 'calls': journal.value['calls'], 'draft': None}
        pin = pin_writer(Path(pin_path), created['order_id'])
        journal.value.update(state='fresh_order_read', configurationId=pin.configuration_id,
                             orderStatus=readback.order_status, paymentTruth=readback.payment_truth,
                             captureStatus=readback.capture_status, currency=readback.currency,
                             amount=readback.amount, orderObservedAt=observed_at)
        journal.save()
        facts = SandboxOrderFacts(order_id=created['order_id'], order_status=readback.order_status,
            payment_truth=readback.payment_truth, capture_status=readback.capture_status,
            currency=readback.currency, amount=readback.amount,
            source='reviewer_live_paypal_sandbox_read', observed_at=observed_at,
            configuration_id=pin.configuration_id, _pin_path=Path(pin_path))
        from local_model import MODEL, MODEL_DIGEST
        from runtime_guard import swap_policy
        policy = getattr(granite_client, 'stable_swap_policy', None)
        fixed_policy = swap_policy(standalone_demo=getattr(granite_client, 'standalone_demo', False))
        journal.value['model'].update(expectedModel=MODEL, expectedDigest=MODEL_DIGEST,
            startedAt=now(), guardPolicy='stable_swap_baseline' if policy else fixed_policy['name'],
            baselineMiB=getattr(policy, 'baseline_mib', None))
        if policy is None:
            journal.value['model']['maxSwapMiB'] = fixed_policy['max_swap_mib']
        journal.value['state'] = 'granite_started'
        journal.value['model'].update(attempted=True, status='started', requestCount=1)
        journal.save()
        try:
            suggestion = generate_sandbox_review(facts, granite_client, question)
        except Exception as error:
            stage, code = getattr(error, 'stage', 'model_request'), getattr(error, 'failure_code', 'MODEL_REQUEST_FAILED')
            journal.value.update(state='granite_failed')
            journal.value['model'].update(status='failed',
                stage=stage if type(stage) is str and SAFE_STAGE.fullmatch(stage) else 'model_request',
                code=code if type(code) is str and SAFE_CODE.fullmatch(code) else 'MODEL_REQUEST_FAILED',
                requestCount=getattr(error, 'attempt_count', 0)
                    if getattr(error, 'attempt_count', 0) in (0, 1) else 0)
            journal.save()
            raise AdapterError('Local Granite draft failed; order remains created and unapproved',
                               stage='model_request', failure_code='LOCAL_DRAFT_FAILED') from None
        journal.value.update(state='draft_pending_human_review')
        journal.value['model'].update(status='draft_ready', completedAt=now(),
            executionVerified=getattr(granite_client, 'execution_verified', False),
            phraseId=suggestion.phrase_id, styleId=suggestion.style_id, questionIntent=suggestion.question_intent, stage=None, code=None,
                                      requestCount=1)
        # The journal is durable recovery metadata, not a content fingerprint.
        # A plain hash of a low-entropy question could be guessed offline.
        journal.save()
        return {'state': 'draft_pending_human_review', 'order_status': readback.order_status,
                'payment_truth': readback.payment_truth, 'observed_at': observed_at,
                'draft': suggestion, 'facts': facts, 'calls': journal.value['calls'],
                'journal': str(Path(journal_path))}
    except AdapterError as error:
        journal.value['state'] = 'stopped'
        journal.value['failure'] = _safe_exception(error)
        journal.save()
        raise
    finally:
        if token is not None:
            del token
        if credentials is not None:
            del credentials


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='start the interactive one-shot Sandbox flow')
    parser.add_argument('--reviewer-order-pin', type=Path,
                        help='new owner-only pin destination; must not exist')
    parser.add_argument('--journal', type=Path,
                        help='new private single-attempt journal; must not exist')
    parser.add_argument('--standalone', action='store_true', help='isolated macOS or supported Linux host; explicit idle confirmation and local model/resource checks required')
    parser.add_argument('--review-ui', action='store_true', help='serve the completed draft for local seller review without another PayPal read or model request')
    parser.add_argument('--review-port', type=int, default=0, help='loopback review port; default 0 chooses an available port')
    parser.add_argument('--stable-swap-baseline', action='store_true')
    parser.add_argument('--resource-proof', type=Path, help='existing reviewed local controller liveness proof, never bundled')
    args = parser.parse_args(argv)
    if not 0 <= args.review_port <= 65535 or (args.review_port and not args.review_ui):
        parser.error('--review-port requires --review-ui and must be between 0 and 65535')
    if not args.run:
        print(json.dumps({'state': 'not_run', 'reason': 'explicit_run_flag_required'}))
        return 0
    if args.reviewer_order_pin is None or args.journal is None:
        parser.error('--run requires explicit --reviewer-order-pin and --journal')
    if (not args.reviewer_order_pin.is_absolute() or not args.journal.is_absolute()
            or args.reviewer_order_pin == args.journal):
        parser.error('pin and journal must be distinct absolute paths')
    if args.standalone and (args.resource_proof is not None or args.stable_swap_baseline):
        parser.error('--standalone cannot use controller proof or the owner swap exception')
    from app import _make_client, _confirm_standalone_demo, _check_startup_lock
    import runtime_guard
    original_proof = runtime_guard.INSPECTED_STALE_PROOF
    policy = None
    from sandbox_ai_review import normalize_payment_question
    try:
        if args.resource_proof is not None:
            if not args.resource_proof.is_absolute():
                parser.error('--resource-proof must be an absolute path')
            from liveness import inspect_proof
            inspect_proof(args.resource_proof, Path.home() / 'SecondBrain/runtime')
            runtime_guard.INSPECTED_STALE_PROOF = args.resource_proof
        if args.stable_swap_baseline:
            from runtime_guard import StableSwapBaseline
            policy = StableSwapBaseline()
        client = _make_client('granite', standalone_demo=args.standalone, stable_swap_policy=policy)
        if args.reviewer_order_pin.exists() or args.reviewer_order_pin.is_symlink():
            print('Stopped: the judge-flow Sandbox pin already exists; no network request made.', file=sys.stderr)
            return 2
        if args.journal.exists() or args.journal.is_symlink():
            print('Stopped: this one-shot flow already has an attempt record; do not rerun.', file=sys.stderr)
            return 2
        question = normalize_payment_question(
            input('Synthetic buyer question (1–500 chars; no real customer personal data): '))
        print('This will create exactly one USD 10.00 PayPal Sandbox order, read that returned order once, then make one local Granite draft.')
        print('It will not approve, capture, refund, send, fulfill, or process a payment. The attempt is one-shot and will not be retried.')
        answer = input('Type CREATE ONE USD 10 SANDBOX ORDER to continue: ')
        if answer != CONFIRMATION:
            return 2
        if args.standalone:
            if not _confirm_standalone_demo(client):
                return 2
        else:
            _check_startup_lock(client)
            client.preflight()  # prompts finished; baseline is fresh before OAuth
        result = run_once(transport=UrllibSandboxTransport(), credentials=load_owner_sandbox_credentials,
            granite_client=client, question=question, pin_path=args.reviewer_order_pin, journal_path=args.journal,
            confirm_create=lambda: answer == CONFIRMATION)
    except (AdapterError, EOFError, KeyboardInterrupt, ValueError, runtime_guard.PilotStop) as error:
        print(json.dumps({'state':'stopped', **_safe_exception(error)}), file=sys.stderr)
        return 2
    finally:
        runtime_guard.INSPECTED_STALE_PROOF = original_proof
        if policy is not None:
            policy.close()
    if result['state'] != 'draft_pending_human_review':
        print('No draft was generated. A Sandbox order may have been created; preserve and inspect the private attempt record before taking further action.')
        return 2
    print('Sandbox order read and local Granite draft are complete.')
    print('Order status: CREATED; payment truth: CREATED_NOT_APPROVED; no capture or send occurred.')
    print('Observed at: ' + result['observed_at'])
    print('Draft status: PENDING HUMAN REVIEW — NOT SENT')
    print(result['draft'].review_text)
    print('Seller next step: ' + result['draft'].seller_next_step)
    print('Private one-shot receipt: ' + result['journal'])
    if args.review_ui:
        from sandbox_review_ui import serve_completed_review
        try:
            state = serve_completed_review(result, client, pin_path=args.reviewer_order_pin,
                                           journal_path=args.journal, port=args.review_port)
        except (OSError, ValueError, runtime_guard.PilotStop):
            print('The Sandbox read and Granite draft completed, but the local review screen could not open. '
                  'Preserve the completed receipt; do not rerun the Sandbox attempt.', file=sys.stderr)
            return 2
        print('Local seller review recorded. Nothing was sent.' if state['review_status'] == 'reviewed_not_sent'
              else 'Seller review remains pending. Nothing was sent.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
