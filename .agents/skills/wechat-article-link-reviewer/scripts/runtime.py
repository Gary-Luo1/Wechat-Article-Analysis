#!/usr/bin/env python3
"""Run skill commands through the isolated virtual environment when installed."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from paths import venv_dir


COMMANDS = {
    "process": "process_pending.py",
    "manage": "manage.py",
}


def _venv_python() -> Path:
    root = venv_dir()
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _system_runtime_is_ready(command: str) -> bool:
    try:
        __import__("curl_cffi")
        __import__("requests")
        __import__("bs4")
    except ImportError:
        return False
    return True


def _interpreter_is_ready(interpreter: Path) -> bool:
    try:
        result = subprocess.run(
            [
                str(interpreter),
                "-c",
                "import bs4, curl_cffi, requests",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] not in COMMANDS:
        print("usage: runtime.py {process|manage} [args...]", file=sys.stderr)
        return 2
    command = args.pop(0)
    script = Path(__file__).resolve().parent / COMMANDS[command]
    preferred = _venv_python()
    if preferred.exists() and _interpreter_is_ready(preferred):
        interpreter = preferred
    elif _system_runtime_is_ready(command):
        interpreter = Path(sys.executable)
    else:
        print(
            "Python dependencies are unavailable; run the repository installer first",
            file=sys.stderr,
        )
        return 1
    result = subprocess.run(
        [str(interpreter), str(script), *args],
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
