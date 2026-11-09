"""Explicit, read-only reconciliation for the one known PayPal sandbox order."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import stat

from private_file_support import supports_private_files
from sandbox_adapter import (AdapterError, SandboxAdapter, SandboxOAuthClient,
                             SafeSandboxCallCounts, UrllibSandboxTransport,
                             load_owner_sandbox_credentials)
from reviewer_sandbox_order import read_order_pin, read_judge_order_receipt

KNOWN_ORDER_ID = 'REDACTEDORDERID'
KNOWN_REQUEST_ID = 'REDACTED_SANDBOX_REQUEST_ID'
RECEIPT_PATH = Path(__file__).resolve().parent / 'evidence' / 'offline-synthetic-receipt.json'
PRIVATE_RUNTIME_PROFILE = Path(__file__).resolve().parent / 'data' / 'private-sandbox-runtime.json'
REDACTED_ORDER_IDS = frozenset({'REDACTEDORDERID', 'REDACTED_ORDER_ID', 'ORDER_ID_REDACTED'})


def read_saved_judge_order_facts(*, pin_path: Path, journal_path: Path) -> dict:
    """Load historical facts from a validated successful journal, without a refresh.

    The validator reads the journal once and checks the identity, USD 10 amount,
    CREATED status, absent capture and all three successful HTTP response stages.
    The returned timestamp is the original read time, never the current time.
    """
    receipt = read_judge_order_receipt(pin_path=pin_path, journal_path=journal_path)
    return {'order_id': receipt.pin.order_id, 'order_status': 'CREATED',
            'payment_truth': 'CREATED_NOT_APPROVED', 'capture_status': None,
            'currency': 'USD', 'amount': '10.00', 'observed_at': receipt.observed_at,
            'source': 'saved_paypal_sandbox_receipt'}


def read_recorded_evidence(path: Path = RECEIPT_PATH) -> dict:
    """Return only validated, allowlisted facts from the private local receipt."""
    try:
        if path.is_symlink():
            raise AdapterError('Sandbox evidence file is not a regular local file')
        info = path.stat()
        if not path.is_file() or info.st_size > 8192:
            raise AdapterError('Sandbox evidence file is unavailable')
        value = json.loads(path.read_bytes())
    except AdapterError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise AdapterError('Sandbox evidence file is unavailable or invalid') from None
    if (type(value) is not dict
            or value.get('schema') != 'one-sandbox-orders-v2-create-read-once-v1'
            or value.get('synthetic') is not True
            or value.get('environment') != 'PayPal sandbox only'
            or value.get('state') != 'created_and_read'
            or value.get('requestId') != KNOWN_REQUEST_ID
            or value.get('orderId') != KNOWN_ORDER_ID
            or value.get('postCount') != 1 or value.get('getCount') != 1
            or value.get('postHttpStatus') != 201 or value.get('getHttpStatus') != 200
            or value.get('identityMatched') is not True
            or value.get('fixedAmountMatched') is not True
            or value.get('capturePresent') is not False
            or value.get('orderStatus') != 'CREATED'
            or value.get('getOrderStatus') != 'CREATED'):
        raise AdapterError('Sandbox evidence does not match the verified one-order receipt')
    created_at = value.get('createdAt')
    if type(created_at) is not str or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', created_at):
        raise AdapterError('Sandbox evidence timestamp is invalid')
    return {'order_id': KNOWN_ORDER_ID, 'order_status': 'CREATED',
            'capture_status': None, 'payment_truth': 'CREATED_NOT_APPROVED',
            'currency': 'USD', 'amount': '10.00', 'observed_at': created_at,
            'source': 'synthetic_order_fixture'}


def read_private_current_order(*, profile_path: Path = PRIVATE_RUNTIME_PROFILE) -> dict:
    """Read a digest-pinned owner-private sandbox receipt; never store its ID in the profile."""
    if not supports_private_files():
        raise AdapterError('Private Sandbox files require supported POSIX ownership and permissions',
                           stage='configuration', failure_code='PRIVATE_FILES_UNSUPPORTED')
    try:
        if profile_path.is_symlink() or profile_path.stat().st_size > 2048:
            raise AdapterError('Private sandbox runtime profile is unavailable')
        if stat.S_IMODE(profile_path.stat().st_mode) & 0o077:
            raise AdapterError('Private sandbox runtime profile permissions are too broad')
        profile = json.loads(profile_path.read_bytes())
        if (type(profile) is not dict or set(profile) != {'schema', 'receipt', 'sha256'}
                or profile.get('schema') != 'private-sandbox-runtime-v1'
                or type(profile.get('receipt')) is not str
                or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}\.json', profile['receipt'])
                or type(profile.get('sha256')) is not str
                or not re.fullmatch(r'[a-f0-9]{64}', profile['sha256'])):
            raise AdapterError('Private sandbox runtime profile is invalid')
        evidence_dir = Path(__file__).resolve().parent / 'evidence'
        receipt = evidence_dir / profile['receipt']
        if receipt.parent != evidence_dir or receipt.is_symlink():
            raise AdapterError('Private sandbox receipt path is invalid')
        if stat.S_IMODE(receipt.stat().st_mode) & 0o077:
            raise AdapterError('Private sandbox receipt permissions are too broad')
        raw = receipt.read_bytes()
        if len(raw) > 8192 or hashlib.sha256(raw).hexdigest() != profile['sha256']:
            raise AdapterError('Private sandbox receipt digest did not match')
        value = json.loads(raw)
    except AdapterError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise AdapterError('Private sandbox runtime profile is unavailable or invalid') from None
    order_id = value.get('orderId') if type(value) is dict else None
    request_id = value.get('requestId') if type(value) is dict else None
    created = value.get('createdAt') if type(value) is dict else None
    if (type(order_id) is not str or not re.fullmatch(r'[A-Z0-9]{12,24}', order_id)
            or type(request_id) is not str or not re.fullmatch(r'[0-9a-f-]{36}', request_id)
            or value.get('schema') != 'one-sandbox-orders-v2-create-read-once-v1'
            or value.get('environment') != 'PayPal sandbox only'
            or value.get('state') != 'created_and_read'
            or value.get('postCount') != 1 or value.get('getCount') != 1
            or value.get('postHttpStatus') != 201 or value.get('getHttpStatus') != 200
            or value.get('identityMatched') is not True
            or value.get('fixedAmountMatched') is not True
            or value.get('capturePresent') is not False
            or value.get('orderStatus') != 'CREATED'
            or value.get('getOrderStatus') != 'CREATED'
            or type(created) is not str
            or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', created)):
        raise AdapterError('Private sandbox receipt does not match a verified one-order result')
    return {'order_id': order_id, 'order_status': 'CREATED', 'capture_status': None,
            'payment_truth': 'CREATED_NOT_APPROVED', 'currency': 'USD', 'amount': '10.00',
            'observed_at': created, 'source': 'saved_paypal_sandbox_receipt'}


def refresh_known_order(*, transport=None, credential_loader=load_owner_sandbox_credentials):
    """Perform explicit OAuth then exactly one read of the receipt-pinned order."""
    call_counts = SafeSandboxCallCounts()
    if KNOWN_ORDER_ID in REDACTED_ORDER_IDS:
        error = AdapterError('The private sandbox order pin is redacted; no request was made',
                             stage='configuration', failure_code='ORDER_PIN_REDACTED')
        error.safe_call_counts = call_counts.snapshot()
        raise error
    try:
        try:
            evidence = read_recorded_evidence()
        except AdapterError:
            error = AdapterError('Saved sandbox receipt failed validation',
                                 stage='receipt_validation', failure_code='SAVED_RECEIPT_INVALID')
            error.safe_call_counts = call_counts.snapshot()
            raise error from None
        selected_transport = transport or UrllibSandboxTransport()
        try:
            credentials = credential_loader()
        except AdapterError:
            error = AdapterError('Sandbox credential input failed validation',
                                 stage='credential_validation',
                                 failure_code='SANDBOX_CREDENTIAL_INVALID')
            error.safe_call_counts = call_counts.snapshot()
            raise error from None
        token = SandboxOAuthClient(selected_transport, credentials, enabled=True,
                                   call_counts=call_counts).fetch_token()
        snapshot = SandboxAdapter(selected_transport, token,
                                  call_counts=call_counts).get_known_sandbox_order(evidence['order_id'])
    except AdapterError as error:
        error.safe_call_counts = error.safe_call_counts or call_counts.snapshot()
        raise
    except Exception:
        error = AdapterError('Sandbox reconciliation failed; no automatic retry',
                             stage='reconciliation', failure_code='SANDBOX_RECONCILIATION_FAILED')
        error.safe_call_counts = call_counts.snapshot()
        raise error from None
    return {'order_id': snapshot.order_id, 'order_status': snapshot.order_status,
            'capture_status': snapshot.capture_status, 'payment_truth': snapshot.payment_truth,
            'currency': snapshot.currency, 'amount': snapshot.amount,
            'source': 'live_paypal_sandbox_read', 'read_only': True,
            'safe_call_counts': call_counts.snapshot(),
            'observed_at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}


def refresh_private_current_order(*, transport=None, credential_loader=load_owner_sandbox_credentials):
    """Explicit one-time OAuth + GET for the owner-private digest-pinned receipt order."""
    call_counts = SafeSandboxCallCounts()
    try:
        evidence = read_private_current_order()
        selected_transport = transport or UrllibSandboxTransport()
        credentials = credential_loader()
        token = SandboxOAuthClient(selected_transport, credentials, enabled=True,
                                   call_counts=call_counts).fetch_token()
        snapshot = SandboxAdapter(selected_transport, token,
                                  call_counts=call_counts).get_known_sandbox_order(evidence['order_id'])
    except AdapterError as error:
        error.safe_call_counts = error.safe_call_counts or call_counts.snapshot()
        raise
    except Exception:
        error = AdapterError('Sandbox reconciliation failed; no automatic retry',
                             stage='reconciliation', failure_code='SANDBOX_RECONCILIATION_FAILED')
        error.safe_call_counts = call_counts.snapshot()
        raise error from None
    return {'order_id': snapshot.order_id, 'order_status': snapshot.order_status,
            'capture_status': snapshot.capture_status, 'payment_truth': snapshot.payment_truth,
            'currency': snapshot.currency, 'amount': snapshot.amount,
            'source': 'live_paypal_sandbox_read', 'read_only': True,
            'safe_call_counts': call_counts.snapshot(),
            'observed_at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}


def refresh_reviewer_order(*, transport=None, credential_loader=load_owner_sandbox_credentials,
                           pin_path=None):
    """Read exactly one reviewer-configured Sandbox order, never create or retry.

    The returned facts are a short-lived in-memory read receipt bound to the
    random configuration ID in the owner-only pin file. No receipt is written
    to the source tree or logs.
    """
    call_counts = SafeSandboxCallCounts()
    try:
        pin = read_order_pin(pin_path) if pin_path is not None else read_order_pin()
    except AdapterError as error:
        error.safe_call_counts = call_counts.snapshot()
        raise
    selected_transport = transport or UrllibSandboxTransport()
    try:
        credentials = credential_loader()
    except AdapterError as error:
        failure = AdapterError('Sandbox credential input failed validation',
                               stage='credential_validation', failure_code='SANDBOX_CREDENTIAL_INVALID',
                               http_status=error.http_status)
        failure.safe_call_counts = call_counts.snapshot()
        raise failure from None
    try:
        token = SandboxOAuthClient(selected_transport, credentials, enabled=True,
                                   call_counts=call_counts).fetch_token()
        snapshot = SandboxAdapter(selected_transport, token,
                                  call_counts=call_counts).get_known_sandbox_order(pin.order_id)
        if (snapshot.order_status != 'CREATED' or snapshot.payment_truth != 'CREATED_NOT_APPROVED'
                or snapshot.capture_status is not None):
            raise AdapterError('Only an unapproved, uncaptured CREATED Sandbox order can be reviewed',
                               stage='order_validation', failure_code='ORDER_STATE_NOT_REVIEWABLE')
    except AdapterError as error:
        error.safe_call_counts = error.safe_call_counts or call_counts.snapshot()
        raise
    except Exception:
        error = AdapterError('Sandbox reconciliation failed; no automatic retry',
                             stage='reconciliation', failure_code='SANDBOX_RECONCILIATION_FAILED')
        error.safe_call_counts = call_counts.snapshot()
        raise error from None
    observed_at = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    return {'order_id': snapshot.order_id, 'order_status': snapshot.order_status,
            'capture_status': snapshot.capture_status, 'payment_truth': snapshot.payment_truth,
            'currency': snapshot.currency, 'amount': snapshot.amount,
            'source': 'reviewer_live_paypal_sandbox_read', 'read_only': True,
            'configuration_id': pin.configuration_id,
            'safe_call_counts': call_counts.snapshot(), 'observed_at': observed_at}
