#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
declare -a CANDIDATES=()
if [[ -n "${WECHAT_ARTICLE_PYTHON:-}" ]]; then
  CANDIDATES=("$WECHAT_ARTICLE_PYTHON")
else
  command -v python3 >/dev/null 2>&1 && CANDIDATES+=("$(command -v python3)")
  command -v python >/dev/null 2>&1 && CANDIDATES+=("$(command -v python)")
fi

PYTHON_BIN=""
for candidate in "${CANDIDATES[@]}"; do
  [[ -x "$candidate" ]] || continue
  "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' || continue
  "$candidate" -c 'import bs4, requests' >/dev/null 2>&1 || continue
  PYTHON_BIN="$candidate"
  break
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.9+ with requests and beautifulsoup4 is required; run the installer or set WECHAT_ARTICLE_PYTHON" >&2
  exit 1
fi

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
exec "$PYTHON_BIN" "$SCRIPT_DIR/runtime.py" "$@"
