"""Owner-operated, no-echo setup for the already-approved sandbox env file.

This helper only creates the exact owner-only file if it does not exist. It
never prints or reads back credential values, never contacts PayPal, and never
overwrites an existing target. Run it from a local terminal attached to the
Mac; do not run through a chat/tool prompt that would capture typed values.
"""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import stat
import sys


TARGET = Path.home() / ".secondbrain-secrets" / "paypal-sandbox.env"
KEYS = ("PAYPAL_SANDBOX_CLIENT_ID", "PAYPAL_SANDBOX_CLIENT_SECRET")


class SetupError(Exception):
    pass


def _verify_parent(parent: Path) -> None:
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SetupError("credential directory is not a real directory")
    if info.st_uid != os.getuid():
        raise SetupError("credential directory is not owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SetupError("credential directory permissions are broader than owner-only")


def _dotenv_value(value: str) -> str:
    if type(value) is not str or not value or len(value) > 4096:
        raise SetupError("credential value is empty or outside the size limit")
    if any(char in value for char in ("\x00", "\r", "\n")):
        raise SetupError("credential value contains a forbidden control character")
    # JSON strings are a dotenv-compatible quoted value form and prevent shell
    # metacharacters from becoming executable when a safe dotenv parser is used.
    return json.dumps(value, ensure_ascii=True)


def create_env_file(target: Path, client_id: str, client_secret: str) -> None:
    _verify_parent(target.parent)
    values = (_dotenv_value(client_id), _dotenv_value(client_secret))
    content = "".join(f"{key}={value}\n" for key, value in zip(KEYS, values)).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            target.unlink()
        except OSError:
            pass
        raise


def main() -> int:
    if TARGET.exists() or TARGET.is_symlink():
        print("Not written: the target already exists. No file was changed.", file=sys.stderr)
        return 2
    try:
        _verify_parent(TARGET.parent)
        client_id = getpass.getpass("PayPal sandbox client ID (hidden): ")
        client_secret = getpass.getpass("PayPal sandbox client secret (hidden): ")
        create_env_file(TARGET, client_id, client_secret)
    except (SetupError, OSError, EOFError, KeyboardInterrupt):
        print("Not written: secure setup did not complete. No credential value was displayed.", file=sys.stderr)
        return 1
    finally:
        # Drop references promptly; Python cannot promise memory zeroization.
        if "client_id" in locals():
            del client_id
        if "client_secret" in locals():
            del client_secret
    print("Created owner-only sandbox credential file; values were not displayed. No network request was made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
