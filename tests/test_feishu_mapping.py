"""Field-mapping invalidation behavior for the Feishu check save path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def configured(home: Path) -> dict:
    from config_store import DEFAULT_CONFIG, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["feishu"].update(
        {
            "destination": "existing",
            "enabled": True,
            "identity": "bot",
            "expected_app_id": "cli_abc",
            "cli_profile": "skill-cli_abc",
            "base_token": "base_token",
            "table_id": "table_id",
        }
    )
    save_config(config)
    return config


def _run_check(monkeypatch: pytest.MonkeyPatch, mapping: dict) -> int:
    import feishu_target
    import process_pending

    monkeypatch.setattr(
        feishu_target,
        "lark_cli_info",
        lambda: {"version": "1.0.69", "compatible": True},
    )
    monkeypatch.setattr(
        feishu_target,
        "preflight_feishu",
        lambda feishu: {
            "identity": "bot",
            "field_count": len(mapping),
            "mapping": mapping,
        },
    )
    return process_pending.cmd_feishu_check(save_mapping=True)


MAPPING_A = {
    "title": {"field_id": "fld_title", "name": "Title", "type": "text"},
    "url": {"field_id": "fld_url", "name": "URL", "type": "url"},
}
MAPPING_B = {
    "title": {"field_id": "fld_other", "name": "Other", "type": "text"},
    "url": {"field_id": "fld_url", "name": "URL", "type": "url"},
}


def test_saving_changed_mapping_updates_the_explicit_sync_target(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    from config_store import load_config, save_config

    configured(tmp_path / "state")
    assert _run_check(monkeypatch, MAPPING_A) == 0
    assert load_config()["feishu"]["field_mapping"] == MAPPING_A
    # Saving the identical mapping again is idempotent.
    assert _run_check(monkeypatch, MAPPING_A) == 0
    # A different mapping replaces the target used by future explicit writes.
    assert _run_check(monkeypatch, MAPPING_B) == 0
    saved = load_config()
    assert saved["feishu"]["field_mapping"] == MAPPING_B
    capsys.readouterr()


def test_completed_review_is_written_as_read():
    from bitable_client import build_record

    record = build_record(
        {
            "title": "Reviewed",
            "account": "Example",
            "link": "https://mp.weixin.qq.com/s/read",
            "update_time": 1_700_000_000,
        },
        {"score": 8, "rationale": "reviewed", "summary": "done"},
    )

    assert record["阅读状态"] == "已读"
