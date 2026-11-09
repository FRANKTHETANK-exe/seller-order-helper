#!/usr/bin/env python3
"""Run the real, one-shot PayPal Sandbox + local Granite judge flow.

No sample/stub fallback, retries, downloads, or credential arguments.
The unchanged official flow obtains its own create confirmation and RUN attestation.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT if (ROOT / "judge_sandbox_flow.py").is_file() else ROOT / "seller-order-helper"
PIN_NAME = "paypal-judge-sandbox-order.json"
JOURNAL_NAME = "paypal-judge-flow-once.json"
CREDENTIAL_NAME = "paypal-sandbox.env"


class LauncherStop(Exception):
    pass


def command(private_dir: Path, *, setup: bool = False) -> list[str]:
    if setup:
        return [sys.executable, "-u", "-B", str(SOURCE / "prepare_paypal_sandbox_env.py")]
    return [sys.executable, "-u", "-B", str(SOURCE / "judge_sandbox_flow.py"),
            "--run", "--standalone", "--review-ui",
            "--reviewer-order-pin", str(private_dir / PIN_NAME),
            "--journal", str(private_dir / JOURNAL_NAME)]


def check_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise LauncherStop("Private setup is missing. Run python3 -B RUN_SANDBOX.py --setup in your local terminal.") from None
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise LauncherStop("The private directory must be a real owner-only directory. Stop and have its owner inspect it; this launcher will not change existing permissions.")


def check_no_prior_attempt(private_dir: Path) -> None:
    # The aliases were documented in the unchanged source README. Preserve them
    # too, rather than accidentally treating a prior run as a fresh judge setup.
    for name in (PIN_NAME, JOURNAL_NAME, "judge-order.json", "judge-attempt.json"):
        try:
            (private_dir / name).lstat()
        except FileNotFoundError:
            continue
        raise LauncherStop(
            "A prior Sandbox pin or attempt record exists (" + name + "). "
            "No flow was started. Preserve it and inspect the prior outcome locally. "
            "Do not delete/reset it, change HOME, choose new filenames, or rerun an uncertain create. "
            "A new run requires a separately reviewed, explicitly authorized attempt.")


def check_credential_metadata(private_dir: Path) -> None:
    try:
        info = (private_dir / CREDENTIAL_NAME).lstat()
    except FileNotFoundError:
        raise LauncherStop("Sandbox credentials are missing. Run python3 -B RUN_SANDBOX.py --setup in your local terminal.") from None
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise LauncherStop("The credential file must be a regular owner-owned 0600 file. Stop and inspect it locally; do not paste values into chat or command arguments.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup", action="store_true",
                      help="explicit local hidden-input credential setup only; no network or model call")
    mode.add_argument("--check", action="store_true",
                      help="check local paths/permissions only; no credential values, network, model, or resource admission")
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 10):
            raise LauncherStop("Python 3.10 or newer is required.")
        if (sys.platform not in ("darwin", "linux") or os.name != "posix"
                or not callable(getattr(os, "getuid", None))
                or not callable(getattr(os, "fchmod", None))):
            raise LauncherStop("Real connected mode requires supported macOS or compatible Linux. Windows is unsupported; there is no stub fallback.")
        for name in ("judge_sandbox_flow.py", "prepare_paypal_sandbox_env.py"):
            if not (SOURCE / name).is_file():
                raise LauncherStop("Use the complete repository checkout, or keep seller-order-helper beside this launcher in the nested kit.")
        private_dir = Path.home() / ".secondbrain-secrets"
        if args.setup:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise LauncherStop("Run --setup yourself in a private local terminal, without piping or recording input.")
            private_dir.mkdir(mode=0o700, exist_ok=True)
        check_private_directory(private_dir)
        check_no_prior_attempt(private_dir)
        if args.setup:
            if (private_dir / CREDENTIAL_NAME).exists() or (private_dir / CREDENTIAL_NAME).is_symlink():
                raise LauncherStop("The credential file already exists. It was not read or changed. Use --check, then launch only if no prior attempt exists.")
        else:
            check_credential_metadata(private_dir)
        if args.check:
            print("Local path and permission checks passed. Credential contents, PayPal, Ollama, the model and resource admission were NOT checked. No network or model call was made.")
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise LauncherStop("Launch from a local interactive terminal so the official confirmations remain visible.")
        if not args.setup:
            print("REAL CONNECTED MODE: after the official confirmations, create one USD 10.00 PayPal Sandbox order, read it once, and request one local Granite draft. No payment, capture or message send.", flush=True)
        args_to_run = command(private_dir, setup=args.setup)
        os.execv(sys.executable, args_to_run)
    except LauncherStop as error:
        print("Stopped: " + str(error), file=sys.stderr)
        return 2
    except (OSError, RuntimeError):
        print("Stopped: local setup or launch could not complete. Preserve all existing files and inspect their ownership/access. No automatic retry.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
