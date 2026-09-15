#!/usr/bin/env bash
# Install everything the matcher needs into a project-local virtualenv.
#   SWW_PYTHON=...        choose the interpreter
#   SWW_NO_EMBEDDINGS=1   skip the semantic retrieval model (BM25 only)
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
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r matcher/requirements.lock -e './matcher[test]'

if [ "${SWW_NO_EMBEDDINGS:-}" = "1" ]; then
  echo 'Skipping semantic retrieval (SWW_NO_EMBEDDINGS=1). Ranking will use BM25 only.'
else
  # Best effort: a failure here leaves a working install that ranks with BM25,
  # so it must not abort the setup.
  if .venv/bin/python -m pip install -r matcher/requirements-embeddings.lock -e './matcher[embeddings]'; then
    .venv/bin/python - <<'PY' || echo 'Model download failed; ranking will use BM25 until it succeeds.'
from sww.embedding import load_embedder
print('Semantic retrieval ready.' if load_embedder() else 'Semantic retrieval unavailable.')
PY
  else
    echo 'Could not install the embeddings extra; ranking will use BM25 only.' >&2
  fi
fi

.venv/bin/python -m playwright install chromium
npm --prefix frontend ci
npm --prefix frontend run build
echo 'Ready. Run ./scripts/start-matcher.sh and open http://127.0.0.1:8765/waterlooworks'
