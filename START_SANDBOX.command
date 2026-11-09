#!/bin/sh
# macOS convenience entry point. The same confirmations are still required.
cd "$(dirname "$0")" || exit 2
exec python3 -u -B RUN_SANDBOX.py "$@"
