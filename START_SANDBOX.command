#!/bin/sh
# macOS convenience entry point. The same confirmations are still required.
cd "$(dirname "$0")" || exit 2
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Stopped: Python 3.10+ is required. Install it yourself from https://www.python.org/downloads/macos/ and reopen Terminal. See README.md.' >&2
    exit 2
fi
exec python3 -u -B RUN_SANDBOX.py "$@"
