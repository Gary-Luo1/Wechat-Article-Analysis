"""Configuration transaction and concurrency tests for the reviewer config store."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "wechat-article-link-reviewer" / "scripts"


def configured() -> dict:
    from config_store import DEFAULT_CONFIG, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["feishu"].update(
        {
            "destination": "existing",
            "enabled": True,
            "identity": "user",
            "expected_app_id": "cli_abc",
            "expected_user_open_id": "ou_user",
            "base_token": "bas_abc",
            "table_id": "tbl_abc",
        }
    )
    save_config(config)
    return config


def test_update_health_preserves_reviewer_target(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    from config_store import load_config, update_health

    configured()
    update_health("feishu", success=True)
    final = load_config()
    assert final["feishu"]["base_token"] == "bas_abc"
    assert final["health"]["feishu"]["last_failure_kind"] == ""


def test_modify_config_validation_failure_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    from config_store import ConfigError, load_config, modify_config

    configured()
    before = load_config()

    def corrupt(config):
        config["setup"]["feishu_authorization"]["state"] = "bogus"

    with pytest.raises(ConfigError):
        modify_config(corrupt)
    assert load_config() == before


def test_v11_unscoped_manager_approval_migrates_to_undecided():
    from config_store import CONFIG_VERSION, DEFAULT_CONFIG, validate_config

    legacy = json.loads(json.dumps(DEFAULT_CONFIG))
    legacy["version"] = 11
    legacy["feishu"]["manager_access"] = "approved"
    legacy["feishu"].pop("manager_access_base_name", None)
    legacy["feishu"].pop("manager_access_table_name", None)

    migrated = validate_config(legacy)
    assert migrated["version"] == CONFIG_VERSION
    assert migrated["feishu"]["manager_access"] == "undecided"


def test_config_lock_serializes_cross_process_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    configured()
    script = (
        "import os, sys\n"
        'sys.path.insert(0, os.environ["REVIEWER_SCRIPTS"])\n'
        "from config_store import modify_config\n"
        "def bump(c):\n"
        '    c["health"]["feishu"]["consecutive_failures"] += 1\n'
        "    return c\n"
        "for _ in range(5): modify_config(bump)\n"
    )
    env = dict(os.environ)
    env["WECHAT_ARTICLE_HOME"] = str(tmp_path / "state")
    env["REVIEWER_SCRIPTS"] = str(SCRIPTS)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for process in processes:
        _, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr

    from config_store import load_config

    assert load_config()["health"]["feishu"]["consecutive_failures"] == 10
