"""Runtime path-resolution tests for the single state-directory rule."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_runtime_venv_derives_from_paths_module(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))

    import runtime
    from paths import venv_dir

    expected = venv_dir() / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert runtime._venv_python() == expected


@pytest.mark.skipif(os.name == "nt", reason="XDG fallback is POSIX behavior")
def test_empty_xdg_state_home_falls_back_like_paths(tmp_path, monkeypatch):
    import runtime
    from paths import APP_NAME, data_dir

    monkeypatch.delenv("WECHAT_ARTICLE_HOME", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "")
    monkeypatch.setattr("paths.sys_platform", lambda: "linux")

    expected = Path.home() / ".local" / "state" / APP_NAME
    assert data_dir() == expected
    assert runtime._venv_python() == expected / "venv" / "bin/python"


def test_runtime_rejects_system_python_without_article_dependencies(monkeypatch):
    import builtins
    import runtime

    original_import = builtins.__import__

    def block_bs4(name, *args, **kwargs):
        if name == "bs4":
            raise ModuleNotFoundError("bs4 is unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_bs4)
    assert runtime._system_runtime_is_ready("process") is False


def test_runtime_rejects_system_python_without_chrome_impersonation(monkeypatch):
    import builtins
    import runtime

    original_import = builtins.__import__

    def block_curl_cffi(name, *args, **kwargs):
        if name == "curl_cffi":
            raise ModuleNotFoundError("curl_cffi is unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_curl_cffi)
    assert runtime._system_runtime_is_ready("process") is False
