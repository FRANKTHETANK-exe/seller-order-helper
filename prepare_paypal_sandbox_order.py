"""No-echo setup for one reviewer-owned PayPal Sandbox order pin.

This writes only a validated order ID and random configuration identity to an
owner-only file. It does not read credentials, contact PayPal, or create an order.
"""
from __future__ import annotations

import getpass
from pathlib import Path
import sys

from reviewer_sandbox_order import DEFAULT_ORDER_PIN, write_order_pin
from sandbox_adapter import AdapterError


def main() -> int:
    if DEFAULT_ORDER_PIN.exists() or DEFAULT_ORDER_PIN.is_symlink():
        print('Not written: an order pin already exists. No file was changed.', file=sys.stderr)
        return 2
    try:
        order_id = getpass.getpass('Existing PayPal Sandbox order ID (hidden): ')
        write_order_pin(DEFAULT_ORDER_PIN, order_id)
    except (AdapterError, EOFError, KeyboardInterrupt):
        print('Not written: secure order-pin setup did not complete. The ID was not displayed.', file=sys.stderr)
        return 1
    finally:
        if 'order_id' in locals():
            del order_id
    print('Created owner-only Sandbox order pin. No credential was read and no network request was made.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
