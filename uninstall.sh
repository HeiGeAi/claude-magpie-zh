#!/bin/bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Error: claude-magpie-zh only supports macOS." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

find_python310() {
  local cand
  for cand in \
    "${PYTHON_BIN:-}" \
    "$(command -v python3 2>/dev/null || true)" \
    "/opt/homebrew/bin/python3" \
    "/usr/local/bin/python3" \
    "$HOME/.local/bin/python3" \
    "$HOME/.pyenv/shims/python3" \
    "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"
  do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

PY="$(find_python310 || true)"
if [ -z "$PY" ]; then
  echo "Error: Python >= 3.10 is required." >&2
  exit 1
fi

exec "$PY" "$SCRIPT_DIR/claude_magpie_zh.py" restore "$@"
