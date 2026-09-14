#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ] || [ ! -f frontend/dist/waterlooworks.html ]; then
  echo 'Run ./scripts/setup-matcher.sh first.' >&2
  exit 1
fi
exec .venv/bin/python -m sww
