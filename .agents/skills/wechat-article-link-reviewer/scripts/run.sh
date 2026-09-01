#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${WECHAT_ARTICLE_HOME:-}" ]]; then
  STATE_HOME="${WECHAT_ARTICLE_HOME/#\~/$HOME}"
elif [[ "$(uname -s)" == "Darwin" ]]; then
  STATE_HOME="$HOME/Library/Application Support/wechat-article-link-reviewer"
else
  STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}/wechat-article-link-reviewer"
fi
declare -a CANDIDATES=()
if [[ -n "${WECHAT_ARTICLE_PYTHON:-}" ]]; then
  CANDIDATES=("$WECHAT_ARTICLE_PYTHON")
else
  CANDIDATES+=("$STATE_HOME/venv/bin/python")
  for command_name in python3.13 python3.12 python3.11 python3.10 python3 python; do
    command -v "$command_name" >/dev/null 2>&1 &&
      CANDIDATES+=("$(command -v "$command_name")")
  done
fi

PYTHON_BIN=""
for candidate in "${CANDIDATES[@]}"; do
  [[ -x "$candidate" ]] || continue
  "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || continue
  "$candidate" -c 'import bs4, curl_cffi, requests' >/dev/null 2>&1 || continue
  PYTHON_BIN="$candidate"
  break
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.10+ with curl_cffi, requests, and beautifulsoup4 is required; run the installer or set WECHAT_ARTICLE_PYTHON" >&2
  echo "To build the isolated runtime manually:" >&2
  echo "  python3.12 -m venv \"$STATE_HOME/venv\"" >&2
  echo "  \"$STATE_HOME/venv/bin/pip\" install -r \"$SCRIPT_DIR/../requirements.txt\"" >&2
  exit 1
fi

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
exec "$PYTHON_BIN" "$SCRIPT_DIR/runtime.py" "$@"
