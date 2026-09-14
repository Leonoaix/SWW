#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN="${SWW_PYTHON:-python3}"
if ! "$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10)' 2>/dev/null; then
  if [ -x /opt/homebrew/bin/python3 ]; then
    PYTHON_BIN=/opt/homebrew/bin/python3
  else
    echo 'Python 3.10+ required. Set SWW_PYTHON to its executable.' >&2
    exit 1
  fi
fi
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -r matcher/requirements.lock -e './matcher[test]'
.venv/bin/python -m playwright install chromium
npm --prefix frontend ci
npm --prefix frontend run build
echo 'Ready. Run ./scripts/start-matcher.sh and open http://127.0.0.1:8765/waterlooworks'
