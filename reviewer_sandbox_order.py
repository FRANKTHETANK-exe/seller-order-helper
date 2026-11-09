"""Owner-only pin for a reviewer-supplied PayPal Sandbox order.

The profile identifies one existing order to read. It contains no credentials,
creates no PayPal order, and is never shipped in the public demo archive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextvars import ContextVar
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone
from uuid import uuid4

from private_file_support import supports_private_files
from sandbox_adapter import AdapterError

DEFAULT_ORDER_PIN = Path.home() / '.secondbrain-secrets' / 'paypal-sandbox-order.json'
DEFAULT_JUDGE_ORDER_PIN = Path.home() / '.secondbrain-secrets' / 'paypal-judge-sandbox-order.json'
DEFAULT_JUDGE_JOURNAL = Path.home() / '.secondbrain-secrets' / 'paypal-judge-flow-once.json'
_ACTIVE_REVIEWER_PIN_PATH = ContextVar('active_reviewer_pin_path', default=None)
REDACTED_IDS = frozenset({'REDACTEDORDERID', 'REDACTED_ORDER_ID', 'ORDER_ID_REDACTED'})
ORDER_ID_RE = re.compile(r'[A-Z0-9]{12,24}\Z')
CONFIG_ID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z')


@dataclass(frozen=True)
class ReviewerOrderPin:
    configuration_id: str
    # Keep the private PayPal reference out of repr()/diagnostic logs.
    order_id: str = field(repr=False)


@dataclass(frozen=True)
class VerifiedJudgeReceipt:
    pin: ReviewerOrderPin
    observed_at: str


@contextmanager
def reviewer_pin_context(path: Path | None):
    """Bind prompt validation to the same local pin used to verify its facts."""
    token = _ACTIVE_REVIEWER_PIN_PATH.set(Path(path) if path is not None else None)
    try:
        yield
    finally:
        _ACTIVE_REVIEWER_PIN_PATH.reset(token)


def active_reviewer_pin_path() -> Path | None:
    """Return the current in-process validation context; never serialize it."""
    return _ACTIVE_REVIEWER_PIN_PATH.get()


def validate_order_id(value: str) -> str:
    if type(value) is not str or not ORDER_ID_RE.fullmatch(value) or value in REDACTED_IDS:
        raise AdapterError('Sandbox order pin failed validation', stage='configuration',
                           failure_code='ORDER_PIN_INVALID')
    return value


def _check_parent(parent: Path) -> None:
    if not supports_private_files():
        raise AdapterError('Private Sandbox files require supported POSIX ownership and permissions',
                           stage='configuration', failure_code='PRIVATE_FILES_UNSUPPORTED')
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise AdapterError('Sandbox order-pin directory is not owner-only',
                           stage='configuration', failure_code='ORDER_PIN_DIRECTORY_INVALID')


def write_order_pin(path: Path, order_id: str, *, configuration_id: str | None = None) -> ReviewerOrderPin:
    """Create a 0600 pin once; never overwrite an existing reviewer profile."""
    order_id = validate_order_id(order_id)
    _check_parent(path.parent)
    config_id = configuration_id or str(uuid4())
    if type(config_id) is not str or not CONFIG_ID_RE.fullmatch(config_id):
        raise AdapterError('Sandbox configuration identity is invalid', stage='configuration',
                           failure_code='CONFIGURATION_ID_INVALID')
    payload = {'schema': 'reviewer-sandbox-order-pin-v1',
               'configurationId': config_id, 'orderId': order_id}
    encoded = (json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise AdapterError('Sandbox order pin already exists; it was not overwritten',
                           stage='configuration', failure_code='ORDER_PIN_EXISTS') from None
    except OSError:
        raise AdapterError('Sandbox order pin could not be created securely',
                           stage='configuration', failure_code='ORDER_PIN_WRITE_FAILED') from None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb', closefd=True) as stream:
            fd = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            path.unlink()
        except OSError:
            pass
        raise AdapterError('Sandbox order pin could not be saved securely',
                           stage='configuration', failure_code='ORDER_PIN_WRITE_FAILED') from None
    return ReviewerOrderPin(config_id, order_id)


def read_order_pin(path: Path = DEFAULT_ORDER_PIN) -> ReviewerOrderPin:
    """Read the configured order ID without returning file or OS error text."""
    try:
        _check_parent(path.parent)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1024):
                raise AdapterError('Sandbox order pin permissions or type are invalid',
                                   stage='configuration', failure_code='ORDER_PIN_PERMISSIONS_INVALID')
            raw = stream.read(1025)
        if len(raw) > 1024:
            raise AdapterError('Sandbox order pin exceeds size limit', stage='configuration',
                               failure_code='ORDER_PIN_SIZE_INVALID')
        value = json.loads(raw.decode('utf-8'))
    except AdapterError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise AdapterError('Sandbox order pin is unavailable or invalid', stage='configuration',
                           failure_code='ORDER_PIN_UNAVAILABLE') from None
    if (type(value) is not dict or set(value) != {'schema', 'configurationId', 'orderId'}
            or value.get('schema') != 'reviewer-sandbox-order-pin-v1'
            or type(value.get('configurationId')) is not str
            or not CONFIG_ID_RE.fullmatch(value['configurationId'])):
        raise AdapterError('Sandbox order pin schema is invalid', stage='configuration',
                           failure_code='ORDER_PIN_SCHEMA_INVALID')
    try:
        order_id = validate_order_id(value['orderId'])
    except (KeyError, AdapterError):
        raise AdapterError('Sandbox order pin failed validation', stage='configuration',
                           failure_code='ORDER_PIN_INVALID') from None
    return ReviewerOrderPin(value['configurationId'], order_id)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def read_judge_order_receipt(*, pin_path: Path, journal_path: Path | None = None,
                            require_completed_model=False) -> VerifiedJudgeReceipt:
    """Verify the dedicated judge pin matches its successful create/read journal.

    This is a local-only preflight. It refuses the legacy reviewer pin or any
    alternate profile and performs no credential access or network call.
    """
    if type(require_completed_model) is not bool:
        raise ValueError('Completed-model requirement must be explicit')
    selected = Path(pin_path)
    explicit_journal = journal_path is not None
    journal_path = Path(journal_path) if explicit_journal else DEFAULT_JUDGE_JOURNAL
    try:
        if selected.is_symlink() or journal_path.is_symlink():
            raise AdapterError('Judge order verification path is linked', stage='configuration',
                               failure_code='JUDGE_BINDING_PATH_INVALID')
        _check_parent(selected.parent)
        _check_parent(journal_path.parent)
        # Require the canonical dedicated judge profile and journal. This
        # prevents an implicit fallback to paypal-sandbox-order.json.
        if ((not explicit_journal and selected.absolute() != DEFAULT_JUDGE_ORDER_PIN.absolute())
                or selected.absolute() == journal_path.absolute()):
            raise AdapterError('The explicit judge pin path is not canonical', stage='configuration',
                               failure_code='JUDGE_PIN_PATH_MISMATCH')
        pin = read_order_pin(selected)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(journal_path, flags)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 32768):
                raise AdapterError('Judge journal permissions or type are invalid',
                                   stage='configuration', failure_code='JUDGE_JOURNAL_INVALID')
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise AdapterError('Judge journal exceeds its size bound', stage='configuration',
                               failure_code='JUDGE_JOURNAL_INVALID')
        journal = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except AdapterError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise AdapterError('Judge order pin/journal is unavailable or invalid',
                           stage='configuration', failure_code='JUDGE_JOURNAL_INVALID') from None

    calls = journal.get('calls') if type(journal) is dict else None
    expected = [('oauth', 'POST', 200), ('order_create', 'POST', 201),
                ('order_get', 'GET', 200)]
    calls_match = (type(calls) is list and len(calls) == len(expected)
        and all(type(call) is dict and call.get('stage') == stage
            and call.get('method') == method and call.get('httpStatus') == status
            and call.get('outcome') == 'response'
            for call, (stage, method, status) in zip(calls, expected)))
    model = journal.get('model') if type(journal) is dict else None
    timestamps_valid = type(journal) is dict and all(
        type(journal.get(field)) is str and len(journal[field]) <= 40
        for field in ('createdAt', 'updatedAt', 'orderObservedAt'))
    if timestamps_valid:
        try:
            for field in ('createdAt', 'updatedAt', 'orderObservedAt'):
                datetime.fromisoformat(journal[field].replace('Z', '+00:00'))
        except (TypeError, ValueError, OverflowError):
            timestamps_valid = False
    if not (type(journal) is dict
            and journal.get('schema') == 'judge-sandbox-create-read-draft-v1'
            and journal.get('environment') == 'PayPal Sandbox only'
            and journal.get('state') in ('fresh_order_read', 'draft_pending_human_review')
            and journal.get('orderId') == pin.order_id
            and journal.get('configurationId') == pin.configuration_id
            and journal.get('createdOrderStatus') == 'CREATED'
            and journal.get('orderStatus') == 'CREATED'
            and journal.get('paymentTruth') == 'CREATED_NOT_APPROVED'
            and journal.get('captureStatus') is None
            and journal.get('currency') == 'USD'
            and journal.get('amount') == '10.00'
            and type(journal.get('idempotencyKey')) is str
            and 1 <= len(journal['idempotencyKey']) <= 64
            and calls_match and type(model) is dict
            and ((journal.get('state') == 'fresh_order_read' and model.get('status') == 'not_started' and model.get('requestCount') == 0)
                or (journal.get('state') == 'draft_pending_human_review' and model.get('status') == 'draft_ready' and model.get('requestCount') == 1 and model.get('attempted') is True))
            and timestamps_valid):
        raise AdapterError('Judge pin does not match a verified fresh create/read journal',
                           stage='configuration', failure_code='JUDGE_PIN_JOURNAL_MISMATCH')
    if require_completed_model:
        from local_model import MODEL, MODEL_DIGEST
        if (journal['state'] != 'draft_pending_human_review'
                or model.get('executionVerified') is not True
                or model.get('expectedModel') != MODEL or model.get('expectedDigest') != MODEL_DIGEST):
            raise AdapterError('Judge journal does not establish a completed pinned-model draft',
                               stage='configuration', failure_code='JUDGE_MODEL_NOT_COMPLETED')
    observed = datetime.fromisoformat(journal['orderObservedAt'].replace('Z', '+00:00'))
    if observed.tzinfo is None:
        raise AdapterError('Judge observation timestamp requires a timezone', stage='configuration',
                           failure_code='JUDGE_JOURNAL_INVALID')
    return VerifiedJudgeReceipt(pin, observed.astimezone(timezone.utc).isoformat(
        timespec='seconds').replace('+00:00', 'Z'))


def validate_judge_order_binding(*, pin_path: Path, journal_path: Path | None = None) -> ReviewerOrderPin:
    """Validate a pin/journal pair without accessing credentials or PayPal."""
    return read_judge_order_receipt(pin_path=pin_path, journal_path=journal_path).pin
