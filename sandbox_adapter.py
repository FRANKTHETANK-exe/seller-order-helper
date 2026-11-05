"""PayPal sandbox boundary. Network use is explicit and disabled by default.

Explicit local Sandbox credential loading is provided; it does not occur on import.
There is no automatic request, capture, refund, delivery or production endpoint.
Tests use injected fakes only.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import os
import socket
import stat
import ssl
import urllib.parse
import urllib.error
import urllib.request
from types import MappingProxyType
from typing import Mapping, Protocol

from facts import AMOUNT, CURRENCY, PRODUCT_ID, PRODUCT_NAME, SELLER_ID, OrderFacts

SANDBOX_BASE_URL = 'https://api-m.sandbox.paypal.com'
CREDENTIAL_ENV_NAMES = ('PAYPAL_SANDBOX_CLIENT_ID', 'PAYPAL_SANDBOX_CLIENT_SECRET',
                        'PAYPAL_SANDBOX_MERCHANT_ID')  # Names only; never read.
PAYPAL_SEARCH_READ_SCOPE = 'https://uri.paypal.com/services/reporting/search/read'
MAX_RESPONSE_BYTES = 32768
OAUTH_TOKEN_URL = SANDBOX_BASE_URL + '/v1/oauth2/token'


class AdapterError(ValueError):
    """Bounded error without response bodies, tokens or credential values."""

    def __init__(self, message, *, stage="validation", failure_code="INVALID_INPUT", http_status=None,
                 underlying_exception_class=None):
        super().__init__(message)
        self.stage = stage
        self.failure_code = failure_code
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        safe_classes = {"TimeoutError", "gaierror", "SSLError", "ConnectionRefusedError",
                        "ConnectionResetError", "OSError", "RuntimeError", "Exception"}
        self.underlying_exception_class = (underlying_exception_class
            if underlying_exception_class in safe_classes else None)
        # Set by the explicit one-order reconciliation wrapper. This contains
        # counts only; never attach request/response bodies, headers, or IDs.
        self.safe_call_counts = None


class SafeSandboxCallCounts:
    """Count only attempted fixed-scope OAuth and pinned-order HTTP calls."""
    __slots__ = ('oauth_attempts', 'pinned_order_get_attempts')

    def __init__(self):
        self.oauth_attempts = 0
        self.pinned_order_get_attempts = 0

    def mark_oauth_attempt(self):
        self.oauth_attempts += 1

    def mark_pinned_order_get_attempt(self):
        self.pinned_order_get_attempts += 1

    def snapshot(self):
        return {'oauth_attempts': self.oauth_attempts,
                'pinned_order_get_attempts': self.pinned_order_get_attempts}


def _safe_transport_exception(error):
    """Return only a fixed error class/code; never return OS or URL text."""
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(cause, socket.gaierror):
        return "gaierror", "SANDBOX_DNS_FAILURE"
    if isinstance(cause, ssl.SSLError):
        return "SSLError", "SANDBOX_TLS_FAILURE"
    if isinstance(cause, ConnectionRefusedError):
        return "ConnectionRefusedError", "SANDBOX_CONNECTION_REFUSED"
    if isinstance(cause, ConnectionResetError):
        return "ConnectionResetError", "SANDBOX_CONNECTION_RESET"
    if isinstance(cause, TimeoutError):
        return "TimeoutError", "SANDBOX_TIMEOUT"
    if isinstance(cause, OSError):
        return "OSError", "SANDBOX_NETWORK_ERROR"
    return "Exception", "SANDBOX_NETWORK_ERROR"


def load_owner_sandbox_credentials(path: Path | None = None) -> SandboxCredentials:
    """Read the exact owner-only sandbox file in process without exposing values."""
    path = path or (Path.home() / '.secondbrain-secrets' / 'paypal-sandbox.env')
    try:
        parent = path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
                or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077):
            raise AdapterError('Sandbox credential directory is not owner-only')
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 8192):
                raise AdapterError('Sandbox credential file permissions or type are invalid')
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise AdapterError('Sandbox credential file exceeds size limit')
        lines = raw.decode('utf-8').splitlines()
        values = {}
        for line in lines:
            if not line or '=' not in line:
                raise AdapterError('Sandbox credential file format is invalid')
            key, encoded = line.split('=', 1)
            if key not in ('PAYPAL_SANDBOX_CLIENT_ID', 'PAYPAL_SANDBOX_CLIENT_SECRET') or key in values:
                raise AdapterError('Sandbox credential file keys are invalid')
            value = json.loads(encoded)
            if type(value) is not str or not value or len(value) > 4096 or any(c in value for c in '\x00\r\n'):
                raise AdapterError('Sandbox credential value is invalid')
            values[key] = value
        if set(values) != {'PAYPAL_SANDBOX_CLIENT_ID', 'PAYPAL_SANDBOX_CLIENT_SECRET'}:
            raise AdapterError('Sandbox credential file is incomplete')
        return SandboxCredentials(values['PAYPAL_SANDBOX_CLIENT_ID'], values['PAYPAL_SANDBOX_CLIENT_SECRET'])
    except AdapterError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise AdapterError('Sandbox credential file is unavailable or invalid') from None


def bounded_transaction_read(transport: HttpTransport, token: AccessTokenResponse, *, now: datetime | None = None):
    """Perform one read-only, page-size-one sandbox transaction search."""
    if not callable(getattr(transport, 'request', None)) or not isinstance(token, AccessTokenResponse):
        raise AdapterError('Validated sandbox token and transport are required')
    scopes = set((token.scope or '').split())
    if PAYPAL_SEARCH_READ_SCOPE not in scopes:
        return {'state': 'read_scope_unavailable', 'recordCount': None}
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise AdapterError('Read window clock must include timezone')
    end = current.astimezone(timezone.utc)
    start = end - timedelta(hours=24)
    params = urllib.parse.urlencode({
        'start_date': start.isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'end_date': end.isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'fields': 'transaction_info', 'page_size': '1', 'page': '1',
    })
    headers = MappingProxyType({'Accept': 'application/json', 'Authorization': 'Bearer ' + token.access_token})
    request = HttpRequest('GET', SANDBOX_BASE_URL + '/v1/reporting/transactions?' + params, headers, None)
    try:
        response = transport.request(request)
    except Exception:
        raise AdapterError('Sandbox read transport failed; no automatic retry', stage='read_transport',
                           failure_code='SANDBOX_TRANSPORT_FAILURE') from None
    if not isinstance(response, HttpResponse) or type(response.status_code) is not int:
        raise AdapterError('Malformed sandbox read response', stage='read_response',
                           failure_code='MALFORMED_HTTP_RESPONSE')
    if response.status_code != 200:
        raise AdapterError('Sandbox read rejected; no retry', stage='read_http',
                           failure_code='SANDBOX_READ_REJECTED', http_status=response.status_code)
    if type(response.body) is not bytes or len(response.body) > MAX_RESPONSE_BYTES:
        raise AdapterError('Sandbox read response exceeds bounds')
    try:
        body = json.loads(response.body)
    except (ValueError, UnicodeError, RecursionError):
        raise AdapterError('Sandbox read response is invalid; no retry') from None
    transactions = body.get('transaction_details') if type(body) is dict else None
    if type(transactions) is not list or len(transactions) > 1:
        raise AdapterError('Sandbox read response has an unexpected shape')
    return {'state': 'read_succeeded', 'recordCount': len(transactions)}


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes | None
    timeout_seconds: int = 5
    authentication: str = field(default='INJECTED_SANDBOX_BEARER', repr=False)


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: bytes = field(repr=False)
    # Only the sanitized PayPal diagnostic identifier may cross the transport.
    debug_id: str | None = field(default=None, repr=False)


def _safe_debug_id(headers) -> str | None:
    try:
        value = headers.get('PayPal-Debug-Id') or headers.get('paypal-debug-id')
    except Exception:
        return None
    return value if type(value) is str and re.fullmatch(r'[A-Za-z0-9-]{1,128}', value) else None


class HttpTransport(Protocol):
    def request(self, request: HttpRequest) -> HttpResponse:
        """Inject an explicitly supplied transport; tests supply only fakes."""
        ...


@dataclass(frozen=True)
class SandboxCredentials:
    """Caller-supplied sandbox credentials; values never appear in repr."""
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    def __post_init__(self):
        if (type(self.client_id) is not str or not self.client_id or len(self.client_id) > 1024
                or type(self.client_secret) is not str or not self.client_secret
                or len(self.client_secret) > 1024):
            raise AdapterError('Sandbox credentials are invalid')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibSandboxTransport:
    """Small TLS-verified HTTPS transport pinned to PayPal's sandbox host.

    This class is inert until request() is called. It does not follow redirects,
    log request/response bodies or retry failures. urllib may honor existing
    proxy settings; this code changes no proxy or security setting.
    """
    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(self, request: HttpRequest) -> HttpResponse:
        if (not isinstance(request, HttpRequest) or not request.url.startswith(SANDBOX_BASE_URL + '/')
                or request.url.split('/', 3)[2] != 'api-m.sandbox.paypal.com'
                or request.method not in ('GET', 'POST') or request.timeout_seconds != 5):
            raise AdapterError('Request is outside the fixed sandbox transport contract')
        raw = urllib.request.Request(request.url, data=request.body,
                                     headers=dict(request.headers), method=request.method)
        try:
            with self._opener.open(raw, timeout=request.timeout_seconds) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                return HttpResponse(response.status, body, _safe_debug_id(response.headers))
        except urllib.error.HTTPError as error:
            # Keep status for bounded caller classification; never expose body/headers.
            try:
                body = error.read(MAX_RESPONSE_BYTES + 1)
            except Exception:
                # A partial/error-body read must not hide the HTTP status or
                # PayPal debug ID that identify the failed request.
                body = b''
            finally:
                try:
                    error.close()
                except Exception:
                    pass
            return HttpResponse(error.code, body, _safe_debug_id(error.headers))
        except TimeoutError:
            raise AdapterError('Sandbox transport failed; no automatic retry', stage='transport',
                               failure_code='SANDBOX_TIMEOUT', underlying_exception_class='TimeoutError') from None
        except (urllib.error.URLError, OSError) as error:
            exception_class, failure_code = _safe_transport_exception(error)
            raise AdapterError('Sandbox transport failed; no automatic retry', stage='transport',
                               failure_code=failure_code,
                               underlying_exception_class=exception_class) from None


class SandboxOAuthClient:
    """Explicit OAuth client. Constructing it sends nothing; fetch_token is opt-in."""
    def __init__(self, transport: HttpTransport, credentials: SandboxCredentials, *, enabled=False,
                 call_counts: SafeSandboxCallCounts | None = None):
        if enabled is not True:
            raise AdapterError('Sandbox network access is disabled by default')
        if not callable(getattr(transport, 'request', None)) or not isinstance(credentials, SandboxCredentials):
            raise TypeError('Explicit transport and caller-supplied sandbox credentials are required')
        self._transport, self._credentials = transport, credentials
        self._call_counts = call_counts

    def fetch_token(self):
        raw = (self._credentials.client_id + ':' + self._credentials.client_secret).encode('utf-8')
        authorization = 'Basic ' + base64.b64encode(raw).decode('ascii')
        request = HttpRequest('POST', OAUTH_TOKEN_URL, MappingProxyType({
            'Accept': 'application/json', 'Authorization': authorization,
            'Content-Type': 'application/x-www-form-urlencoded'},),
            b'grant_type=client_credentials', authentication='EXPLICIT_SANDBOX_OAUTH')
        try:
            if self._call_counts is not None:
                self._call_counts.mark_oauth_attempt()
            response = self._transport.request(request)
        except AdapterError as error:
            raise AdapterError('Sandbox OAuth transport failed; no automatic retry', stage='oauth_transport',
                               failure_code=error.failure_code,
                               underlying_exception_class=error.underlying_exception_class) from None
        except Exception as error:
            raise AdapterError('Sandbox OAuth transport failed; no automatic retry', stage='oauth_transport',
                               failure_code='SANDBOX_TRANSPORT_FAILURE',
                               underlying_exception_class=type(error).__name__) from None
        if not isinstance(response, HttpResponse) or type(response.status_code) is not int:
            raise AdapterError('Malformed OAuth HTTP response', stage='oauth_response',
                               failure_code='MALFORMED_HTTP_RESPONSE')
        if response.status_code != 200:
            raise AdapterError('Sandbox OAuth rejected the request; no retry', stage='oauth_http',
                               failure_code='OAUTH_HTTP_REJECTED', http_status=response.status_code)
        try:
            return parse_access_token_response(response.body)
        except AdapterError:
            raise AdapterError('Sandbox OAuth response was invalid; no retry', stage='oauth_response',
                               failure_code='INVALID_OAUTH_RESPONSE') from None


@dataclass(frozen=True)
class AccessTokenResponse:
    """Parsed token response; the opaque token is omitted from repr output."""
    access_token: str = field(repr=False)
    expires_in: int
    scope: str | None = None


def parse_access_token_response(body):
    """Validate a bounded OAuth token fixture/response without logging its token."""
    if type(body) is not bytes or len(body) > MAX_RESPONSE_BYTES:
        raise AdapterError('Token response body type or size is invalid')
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise AdapterError('Token response is not valid bounded JSON') from None
    if type(value) is not dict:
        raise AdapterError('Token response must be an object')
    token, token_type, expires = value.get('access_token'), value.get('token_type'), value.get('expires_in')
    scope = value.get('scope')
    if type(token) is not str or not token or len(token) > 8192:
        raise AdapterError('Token response has an invalid access token')
    if type(token_type) is not str or token_type.casefold() != 'bearer':
        raise AdapterError('Token response has an unsupported token type')
    if type(expires) is not int or expires <= 0 or expires > 2**31 - 1:
        raise AdapterError('Token response has an invalid expiry interval')
    if scope is not None and (type(scope) is not str or len(scope) > 8192):
        raise AdapterError('Token response has an invalid scope')
    return AccessTokenResponse(token, expires, scope)


def money():
    return {'currency_code': CURRENCY, 'value': AMOUNT}


def create_payload():
    """Fixed synthetic seller, one product, quantity one; no buyer information."""
    return {'intent': 'CAPTURE', 'purchase_units': [{
        'reference_id': PRODUCT_ID, 'payee': {'merchant_id': SELLER_ID},
        'amount': {**money(), 'breakdown': {'item_total': money()}},
        'items': [{'name': PRODUCT_NAME, 'sku': PRODUCT_ID,
                   'quantity': '1', 'unit_amount': money()}],
    }]}


def _money_matches(value):
    return (type(value) is dict and type(value.get('currency_code')) is str
            and value.get('currency_code') == CURRENCY
            and type(value.get('value')) is str and value.get('value') == AMOUNT)


def _check_capture_binding_and_conflicts(capture, expected_order_id):
    """Reject explicit contradictory capture facts; do not infer refund history."""
    supplementary = capture.get('supplementary_data', {})
    if type(supplementary) is not dict or type(supplementary.get('related_ids', {})) is not dict:
        raise AdapterError('Capture relation shape is unsupported', stage='order_validation', failure_code='CAPTURE_ORDER_BINDING_INVALID')
    related = supplementary.get('related_ids', {})
    if 'order_id' in related and related['order_id'] != expected_order_id:
        raise AdapterError('Capture is bound to a different order', stage='order_validation', failure_code='CAPTURE_ORDER_ID_MISMATCH')
    details = capture.get('status_details', {})
    if type(details) is not dict:
        raise AdapterError('Capture detail shape is unsupported', stage='order_validation', failure_code='CAPTURE_DETAILS_INVALID')
    if capture.get('status') == 'COMPLETED' and details.get('reason') in ('REFUNDED', 'CHARGEBACK'):
        raise AdapterError('Capture completion conflicts with adjustment evidence', stage='order_validation', failure_code='CAPTURE_ADJUSTMENT_CONFLICT')


def parse_order(value):
    """Select only fixed synthetic order facts; links and buyer fields ignored."""
    try:
        if type(value) is not dict:
            raise AdapterError('Order response must be an object')
        units = value.get('purchase_units')
        if type(units) is not list or len(units) != 1 or type(units[0]) is not dict:
            raise AdapterError('Exactly one purchase unit is required')
        unit = units[0]
        if unit.get('reference_id') != PRODUCT_ID or unit.get('payee') != {'merchant_id': SELLER_ID}:
            raise AdapterError('Response does not match the fixed synthetic seller/product')
        if not _money_matches(unit.get('amount')):
            raise AdapterError('Unexpected amount or currency')
        items = unit.get('items')
        if type(items) is not list or len(items) != 1 or type(items[0]) is not dict:
            raise AdapterError('Exactly one fixed product is required')
        item = items[0]
        if (item.get('sku') != PRODUCT_ID or item.get('name') != PRODUCT_NAME
                or item.get('quantity') != '1' or not _money_matches(item.get('unit_amount'))):
            raise AdapterError('Response item differs from the fixed product')
        capture_status = None
        payments = unit.get('payments', {})
        if type(payments) is not dict:
            raise AdapterError('Malformed payment facts')
        refunds = payments.get('refunds', [])
        if type(refunds) is not list or refunds:
            raise AdapterError('Refund facts require separate reconciliation; no confirmation permitted',
                               stage='order_validation', failure_code='REFUND_FACTS_UNSUPPORTED')
        captures = payments.get('captures', [])
        if type(captures) is not list or len(captures) > 1:
            raise AdapterError('Unsupported capture count')
        if captures:
            capture = captures[0]
            if type(capture) is not dict or not _money_matches(capture.get('amount')):
                raise AdapterError('Unexpected capture amount or currency')
            _check_capture_binding_and_conflicts(capture, value.get('id'))
            capture_status = capture.get('status')
            if capture_status not in ('PENDING', 'COMPLETED'):
                raise AdapterError('Unsupported capture state; no reply suggestion permitted')
        return OrderFacts(order_id=value.get('id'), status=value.get('status'),
                          capture_status=capture_status)
    except AdapterError:
        raise
    except (ValueError, TypeError, KeyError):
        raise AdapterError('Unsupported or inconsistent synthetic order facts') from None


class SandboxAdapter:
    def __init__(self, transport: HttpTransport, access_token: AccessTokenResponse | None = None,
                 *, call_counts: SafeSandboxCallCounts | None = None):
        if not callable(getattr(transport, 'request', None)):
            raise TypeError('An explicit fake transport is required for offline use')
        if access_token is not None and not isinstance(access_token, AccessTokenResponse):
            raise TypeError('Provide a validated opaque bearer token response')
        self._transport = transport
        self._access_token = access_token
        self._call_counts = call_counts

    def _exchange_value(self, method, path, body=None, request_id=None):
        if self._access_token is None:
            raise AdapterError('Sandbox authorization is required before transport')
        headers = {'Accept': 'application/json'}
        headers['Authorization'] = 'Bearer ' + self._access_token.access_token
        encoded = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            encoded = json.dumps(body, separators=(',', ':')).encode('utf-8')
        if request_id is not None:headers['PayPal-Request-Id'] = request_id
        request = HttpRequest(method, SANDBOX_BASE_URL + path,
                              MappingProxyType(headers), encoded)
        try:
            if (self._call_counts is not None and method == 'GET'
                    and re.fullmatch(r'/v2/checkout/orders/[A-Z0-9]{1,36}', path)):
                self._call_counts.mark_pinned_order_get_attempt()
            response = self._transport.request(request)
        except AdapterError as error:
            # Preserve only bounded classification fields, never transport text.
            stage = error.stage if type(error.stage) is str and re.fullmatch(r'[A-Za-z0-9_]{1,32}', error.stage) else 'order_get_transport'
            code = error.failure_code if type(error.failure_code) is str and re.fullmatch(r'[A-Z0-9_]{1,64}', error.failure_code) else 'SANDBOX_NETWORK_FAILURE'
            raise AdapterError('Pinned-order transport failed; no automatic retry',
                               stage=stage, failure_code=code,
                               http_status=error.http_status,
                               underlying_exception_class=error.underlying_exception_class) from None
        except TimeoutError:
            raise AdapterError('Pinned-order read timed out; no automatic retry',
                               stage='order_get_transport', failure_code='SANDBOX_TIMEOUT',
                               underlying_exception_class='TimeoutError') from None
        except Exception as error:
            exception_class, failure_code = _safe_transport_exception(error)
            raise AdapterError('Pinned-order transport failed; no automatic retry',
                               stage='order_get_transport', failure_code=failure_code,
                               underlying_exception_class=exception_class) from None
        if not isinstance(response, HttpResponse) or type(response.status_code) is not int:
            if method == 'GET' and re.fullmatch(r'/v2/checkout/orders/[A-Z0-9]{1,36}', path):
                raise AdapterError('Pinned-order HTTP response was malformed',
                                   stage='order_get_response', failure_code='MALFORMED_HTTP_RESPONSE')
            raise AdapterError('Malformed HTTP response')
        allowed = (201,) if method == 'POST' else (200,)
        if response.status_code not in allowed:
            if method == 'POST' and path == '/v2/checkout/orders':
                raise AdapterError('Sandbox order creation was rejected; no retry',
                                   stage='order_create_http', failure_code='ORDER_CREATE_REJECTED',
                                   http_status=response.status_code)
            if method == 'GET' and re.fullmatch(r'/v2/checkout/orders/[A-Z0-9]{1,36}', path):
                raise AdapterError('Pinned-order read was rejected; no redirect or retry',
                                   stage='order_get_http', failure_code='PINNED_ORDER_GET_REJECTED',
                                   http_status=response.status_code)
            raise AdapterError('Unexpected HTTP status; no redirect or retry')
        if type(response.body) is not bytes or len(response.body) > MAX_RESPONSE_BYTES:
            if method == 'GET' and re.fullmatch(r'/v2/checkout/orders/[A-Z0-9]{1,36}', path):
                raise AdapterError('Pinned-order response body failed size validation',
                                   stage='order_get_response', failure_code='ORDER_RESPONSE_SIZE_INVALID')
            raise AdapterError('Response body type or size is invalid')
        try:
            value = json.loads(response.body)
        except (ValueError, UnicodeError, RecursionError):
            if method == 'GET' and re.fullmatch(r'/v2/checkout/orders/[A-Z0-9]{1,36}', path):
                raise AdapterError('Pinned-order response JSON failed validation',
                                   stage='order_get_response', failure_code='ORDER_RESPONSE_JSON_INVALID') from None
            raise AdapterError('Response is not valid bounded JSON') from None
        return value

    def _exchange(self, method, path, body=None, request_id=None):
        return parse_order(self._exchange_value(method, path, body, request_id))

    def create_order(self, request_id):
        if type(request_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id):
            raise AdapterError('A bounded idempotency request identifier is required')
        return self._exchange('POST', '/v2/checkout/orders', create_payload(), request_id)

    def get_order(self, order_id):
        if type(order_id) is not str or not re.fullmatch(r'[A-Z0-9_-]{1,64}', order_id):
            raise AdapterError('Invalid order identifier')
        facts = self._exchange('GET', '/v2/checkout/orders/' + order_id)
        if facts.order_id != order_id:
            raise AdapterError('Returned order identity does not match request')
        return facts

    def create_first_party_test_order(self, request_id):
        """Create one fixed USD 10 Sandbox test order for a judge-owned app.

        This is available only when an explicit caller invokes it. The caller
        must persist an idempotency key before transport and ask for operator
        confirmation; no UI or default startup invokes this method.
        """
        if type(request_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id):
            raise AdapterError('A bounded idempotency request identifier is required',
                               stage='configuration', failure_code='IDEMPOTENCY_KEY_INVALID')
        payload = create_payload()
        # Use the authenticated first-party app context; the demo seller ID is
        # only a synthetic placeholder and must never be sent to PayPal.
        payload['purchase_units'][0].pop('payee', None)
        result = self._exchange_value('POST', '/v2/checkout/orders', payload, request_id)
        order_id = result.get('id') if type(result) is dict else None
        status = result.get('status') if type(result) is dict else None
        if (type(order_id) is not str or not re.fullmatch(r'[A-Z0-9]{12,24}', order_id)
                or type(status) is not str or status not in
                ('CREATED', 'PAYER_ACTION_REQUIRED', 'APPROVED', 'COMPLETED', 'VOIDED')):
            raise AdapterError('Sandbox create response failed validation; no follow-up action',
                               stage='order_create_response', failure_code='ORDER_CREATE_RESPONSE_INVALID')
        return {'order_id': order_id, 'order_status': status}

    def get_known_sandbox_order(self, order_id):
        """Read a known sandbox order without imposing the mock seller identity."""
        if type(order_id) is not str or not re.fullmatch(r'[A-Z0-9]{1,36}', order_id):
            raise AdapterError('Invalid sandbox order identifier')
        value = self._exchange_value('GET', '/v2/checkout/orders/' + order_id)
        return parse_known_sandbox_order(value, expected_id=order_id)


@dataclass(frozen=True)
class SandboxOrderSnapshot:
    order_id: str
    order_status: str
    currency: str
    amount: str
    capture_status: str | None
    payment_truth: str


def parse_known_sandbox_order(value, *, expected_id):
    """Project only the fixed test order's verified status and payment facts."""
    if type(expected_id) is not str or not re.fullmatch(r'[A-Z0-9]{1,36}', expected_id):
        raise AdapterError('Pinned order identifier failed validation', stage='order_validation',
                           failure_code='PINNED_ORDER_ID_INVALID')
    if type(value) is not dict or value.get('id') != expected_id:
        raise AdapterError('Sandbox order identity failed validation', stage='order_validation',
                           failure_code='ORDER_ID_MISMATCH')
    status = value.get('status')
    if status not in ('CREATED', 'PAYER_ACTION_REQUIRED', 'APPROVED', 'COMPLETED', 'VOIDED'):
        raise AdapterError('Sandbox order status failed validation', stage='order_validation',
                           failure_code='ORDER_STATUS_UNSUPPORTED')
    units = value.get('purchase_units')
    if type(units) is not list or len(units) != 1 or type(units[0]) is not dict:
        raise AdapterError('Sandbox purchase-unit shape failed validation', stage='order_validation',
                           failure_code='PURCHASE_UNIT_SHAPE_INVALID')
    unit = units[0]
    if not _money_matches(unit.get('amount')):
        raise AdapterError('Sandbox amount/currency failed validation', stage='order_validation',
                           failure_code='ORDER_AMOUNT_CURRENCY_MISMATCH')
    # Payee was intentionally omitted from the approved first-party-app test.
    # If item details are returned, validate rather than display them.
    items = unit.get('items')
    if items is not None:
        if type(items) is not list or len(items) != 1 or type(items[0]) is not dict:
            raise AdapterError('Sandbox item shape failed validation', stage='order_validation',
                               failure_code='ORDER_ITEM_SHAPE_INVALID')
        item = items[0]
        if (item.get('sku') != PRODUCT_ID or item.get('name') != PRODUCT_NAME
                or item.get('quantity') != '1' or not _money_matches(item.get('unit_amount'))):
            raise AdapterError('Sandbox item failed validation', stage='order_validation',
                               failure_code='ORDER_ITEM_MISMATCH')
    payments = unit.get('payments', {})
    if type(payments) is not dict:
        raise AdapterError('Sandbox payment facts failed validation', stage='order_validation',
                           failure_code='PAYMENT_FACTS_SHAPE_INVALID')
    refunds = payments.get('refunds', [])
    if type(refunds) is not list or refunds:
        raise AdapterError('Refund facts require separate reconciliation; no confirmation permitted',
                           stage='order_validation', failure_code='REFUND_FACTS_UNSUPPORTED')
    captures = payments.get('captures', [])
    if type(captures) is not list or len(captures) > 1:
        raise AdapterError('Sandbox capture count failed validation', stage='order_validation',
                           failure_code='CAPTURE_COUNT_UNSUPPORTED')
    capture_status = None
    if captures:
        capture = captures[0]
        if type(capture) is not dict or not _money_matches(capture.get('amount')):
            raise AdapterError('Sandbox capture amount/currency failed validation', stage='order_validation',
                               failure_code='CAPTURE_AMOUNT_CURRENCY_MISMATCH')
        _check_capture_binding_and_conflicts(capture, expected_id)
        capture_status = capture.get('status')
        if capture_status not in ('PENDING', 'COMPLETED'):
            raise AdapterError('Sandbox capture status failed validation', stage='order_validation',
                               failure_code='CAPTURE_STATUS_UNSUPPORTED')
    if status == 'VOIDED':
        truth = 'VOIDED'
    elif capture_status == 'COMPLETED':
        truth = 'CAPTURED'
    elif capture_status == 'PENDING':
        truth = 'CAPTURE_PENDING'
    elif status == 'PAYER_ACTION_REQUIRED':
        truth = 'PAYER_ACTION_REQUIRED'
    elif status == 'APPROVED':
        truth = 'APPROVED_WITHOUT_CAPTURE_EVIDENCE'
    elif status == 'COMPLETED':
        truth = 'COMPLETED_WITHOUT_CAPTURE_EVIDENCE'
    else:
        truth = 'CREATED_NOT_APPROVED'
    return SandboxOrderSnapshot(expected_id, status, CURRENCY, AMOUNT, capture_status, truth)
