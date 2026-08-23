from __future__ import annotations

import io
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "wechat-article-link-reviewer" / "scripts"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))


def _article(*, title: str = "Direct article", account: str = "New Account") -> dict:
    return {
        "title": title,
        "account": account,
        "account_id": "biz-direct",
        "digest": "Direct digest",
        "update_time": 1_700_000_000,
        "link": "https://mp.weixin.qq.com/s/direct",
        "text": "Untrusted body",
    }


def _dimensions() -> str:
    return json.dumps(
        {
            "技术深度": 8,
            "信息新颖度": 8,
            "分析深度与独立观点": 8,
            "实用参考价值": 8,
            "内容质量与可信度": 8,
        },
        ensure_ascii=False,
    )


def test_evaluate_link_without_config_fetches_once_and_can_complete(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import process_pending
    from queue_helpers import get_pending

    fetched: list[str] = []

    def fetch(url: str, **_kwargs: object) -> dict:
        fetched.append(url)
        return _article()

    monkeypatch.setattr(process_pending, "fetch_article", fetch)
    assert process_pending.main(["--format", "json", "evaluate", "--url", _article()["link"]]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["status"] == "queued"
    assert payload["data"]["article"]["read_state"]["status"] == "verified"
    assert payload["data"]["untrusted_article_content"] == "Untrusted body"
    assert fetched == [_article()["link"]]
    assert "text" not in get_pending()[0]

    assert process_pending.main(["done", "--link", _article()["link"], "--dims", _dimensions()]) == 0
    assert "Completed: Direct article" in capsys.readouterr().out


def test_evaluate_processed_link_uses_local_result_without_fetching(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import process_pending

    monkeypatch.setattr(process_pending, "fetch_article", lambda *_args, **_kwargs: _article())
    assert process_pending.main(["evaluate", "--url", _article()["link"]]) == 0
    capsys.readouterr()
    assert process_pending.main(["done", "--link", _article()["link"], "--dims", _dimensions()]) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        process_pending,
        "fetch_article",
        lambda *_args, **_kwargs: pytest.fail("processed links must not fetch again"),
    )
    assert process_pending.main(["--format", "json", "evaluate", "--url", _article()["link"]]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "already_processed"


def test_evaluate_pending_link_refreshes_metadata_and_preserves_inbox_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import process_pending
    from queue_helpers import get_pending, update_inbox_item

    monkeypatch.setattr(process_pending, "fetch_article", lambda *_args, **_kwargs: _article(title="Old"))
    assert process_pending.main(["evaluate", "--url", _article()["link"]]) == 0
    capsys.readouterr()
    update_inbox_item(_article()["link"], favorite=True, state="later")
    monkeypatch.setattr(process_pending, "fetch_article", lambda *_args, **_kwargs: _article(title="Fresh"))
    assert process_pending.main(["--format", "json", "evaluate", "--url", _article()["link"]]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["article"]["title"] == "Fresh"
    refreshed = get_pending()[0]
    assert refreshed["title"] == "Fresh"
    assert refreshed["favorite"] is True
    assert refreshed["inbox_state"] == "later"


def test_evaluate_rejects_empty_body_without_queueing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import process_pending
    from queue_helpers import get_pending

    monkeypatch.setattr(process_pending, "fetch_article", lambda *_args, **_kwargs: {**_article(), "text": ""})
    assert process_pending.main(["--format", "json", "evaluate", "--url", _article()["link"]]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENT"
    assert get_pending() == []


def test_removed_commands_are_not_exposed(capsys: pytest.CaptureFixture[str]):
    import manage
    import process_pending
    import runtime

    assert "discover" not in runtime.COMMANDS
    assert "setup" not in runtime.COMMANDS
    assert "lark" not in runtime.COMMANDS
    assert "manage" in runtime.COMMANDS
    with pytest.raises(SystemExit):
        process_pending.build_parser().parse_args(["ingest", "--url", _article()["link"]])
    with pytest.raises(SystemExit):
        process_pending.build_parser().parse_args(["done", "--index", "1"])
    with pytest.raises(SystemExit):
        manage.build_parser().parse_args(["subscriptions", "list"])
    with pytest.raises(SystemExit):
        manage.build_parser().parse_args(
            ["feishu-grant-manager", "--token", "bas_secret", "--type", "bitable"]
        )
    report, next_action = manage._doctor(online=True, save_resolved=False)
    assert report["mode"] == "user_supplied_link_only"
    assert report["setup_stage"] == "link_review_ready"
    assert next_action == "provide_article_link"


def test_done_only_syncs_when_explicitly_requested(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import process_pending

    monkeypatch.setattr(process_pending, "fetch_article", lambda *_args, **_kwargs: _article())
    assert process_pending.main(["evaluate", "--url", _article()["link"]]) == 0
    capsys.readouterr()
    monkeypatch.setattr(process_pending, "_sync_entry", lambda *_args, **_kwargs: pytest.fail("sync was not requested"))
    assert process_pending.main(["done", "--link", _article()["link"], "--dims", _dimensions()]) == 0


def _save_existing_feishu_config(*, min_score: float = 6.0) -> None:
    from config_store import DEFAULT_CONFIG, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["feishu"].update(
        {
            "destination": "existing",
            "enabled": True,
            "identity": "user",
            "expected_app_id": "cli_test",
            "cli_profile": "wechat-article-cli_test",
            "base_token": "bascn_old",
            "table_id": "tbl_old",
            "provisioning": "existing",
            "field_mapping": {"title": {"name": "文章标题"}},
        }
    )
    config["settings"]["min_score"] = min_score
    save_config(config)


def test_existing_feishu_target_requires_preview_then_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import manage
    from config_store import load_config

    _save_existing_feishu_config()
    url = "https://example.feishu.cn/base/bascn_new?table=tbl_new&view=vew_1"
    monkeypatch.setattr(manage.sys, "stdin", io.StringIO(url))
    assert manage.main(["feishu-target", "--url-stdin"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["data"]["configured"] is False
    assert preview["next_action"] == "rerun_with_yes"
    assert "bascn_new" not in json.dumps(preview)
    assert "tbl_new" not in json.dumps(preview)
    assert load_config()["feishu"]["base_token"] == "bascn_old"

    monkeypatch.setattr(manage.sys, "stdin", io.StringIO(url))
    assert manage.main(["feishu-target", "--url-stdin", "--yes"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["configured"] is True
    assert result["data"]["resource_tokens_included"] is False
    assert "bascn_new" not in json.dumps(result)
    assert "tbl_new" not in json.dumps(result)
    saved = load_config()
    assert saved["feishu"]["base_token"] == "bascn_new"
    assert saved["feishu"]["table_id"] == "tbl_new"
    assert saved["feishu"]["field_mapping"] == {}


@pytest.mark.parametrize(
    "url",
    [
        "http://example.feishu.cn/base/bascn_a?table=tbl_a",
        "https://feishu.cn.evil.example/base/bascn_a?table=tbl_a",
        "https://example.feishu.cn/wiki/bascn_a?table=tbl_a",
        "https://example.feishu.cn/base/bascn_a/extra?table=tbl_a",
        "https://example.feishu.cn/base/bascn_a",
        "https://example.feishu.cn/base/bascn_a?table=tbl_a&table=tbl_b",
    ],
)
def test_existing_feishu_target_rejects_unsafe_or_ambiguous_urls(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    import manage
    from config_store import load_config

    _save_existing_feishu_config()
    monkeypatch.setattr(manage.sys, "stdin", io.StringIO(url))
    assert manage.main(["feishu-target", "--url-stdin", "--yes"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert load_config()["feishu"]["base_token"] == "bascn_old"


def test_processed_article_can_be_synced_by_explicit_link_without_refetch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import process_pending
    from queue_helpers import get_processed_entry, update_sync_status

    monkeypatch.setattr(process_pending, "fetch_article", lambda *_args, **_kwargs: _article())
    assert process_pending.main(["evaluate", "--url", _article()["link"]]) == 0
    capsys.readouterr()
    assert process_pending.main(["done", "--link", _article()["link"], "--dims", _dimensions()]) == 0
    capsys.readouterr()
    _save_existing_feishu_config()
    synced: list[str] = []

    def sync_one(entry: dict, *, dry_run: bool = False) -> None:
        assert dry_run is False
        assert entry["sync_status"] == "not_requested"
        synced.append(entry["article"]["link"])
        update_sync_status(entry["article"]["link"], "synced")

    monkeypatch.setattr(process_pending, "_sync_entry", sync_one)
    monkeypatch.setattr(
        process_pending,
        "fetch_article",
        lambda *_args, **_kwargs: pytest.fail("processed sync must not refetch"),
    )
    assert process_pending.main(["sync-feishu", "--link", _article()["link"]]) == 0
    assert synced == [_article()["link"]]
    assert get_processed_entry(_article()["link"])["sync_status"] == "synced"


def test_processed_low_score_requires_per_article_force(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
):
    import process_pending
    from queue_helpers import get_processed_entry, update_sync_status

    low_dimensions = json.dumps(
        {
            "技术深度": 1,
            "信息新颖度": 1,
            "分析深度与独立观点": 1,
            "实用参考价值": 1,
            "内容质量与可信度": 1,
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(process_pending, "fetch_article", lambda *_args, **_kwargs: _article())
    assert process_pending.main(["evaluate", "--url", _article()["link"]]) == 0
    capsys.readouterr()
    _save_existing_feishu_config(min_score=6.0)
    assert process_pending.main(
        ["done", "--link", _article()["link"], "--dims", low_dimensions, "--feishu"]
    ) == 0
    capsys.readouterr()
    assert get_processed_entry(_article()["link"])["sync_status"] == "skipped_low_score"
    monkeypatch.setattr(
        process_pending,
        "_sync_entry",
        lambda *_args, **_kwargs: pytest.fail("below-threshold sync requires force"),
    )
    assert process_pending.main(["sync-feishu", "--link", _article()["link"]]) == 1
    assert "--force-feishu" in caplog.text

    calls: list[bool] = []
    monkeypatch.setattr(
        process_pending,
        "_sync_entry",
        lambda entry, *, dry_run=False: (
            calls.append(dry_run),
            update_sync_status(entry["article"]["link"], "synced"),
        ),
    )
    assert (
        process_pending.main(
            ["sync-feishu", "--link", _article()["link"], "--force-feishu"]
        )
        == 0
    )
    assert calls == [False]
    assert get_processed_entry(_article()["link"])["sync_status"] == "synced"


def test_processed_pending_article_retries_by_link_and_failure_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
):
    import process_pending
    from bitable_client import LarkCLIError
    from queue_helpers import get_processed_entry, update_sync_status

    monkeypatch.setattr(process_pending, "fetch_article", lambda *_args, **_kwargs: _article())
    assert process_pending.main(["evaluate", "--url", _article()["link"]]) == 0
    capsys.readouterr()
    assert process_pending.main(["done", "--link", _article()["link"], "--dims", _dimensions()]) == 0
    capsys.readouterr()
    _save_existing_feishu_config()
    update_sync_status(_article()["link"], "pending", "earlier failure")
    monkeypatch.setattr(
        process_pending,
        "fetch_article",
        lambda *_args, **_kwargs: pytest.fail("pending retry must not refetch"),
    )
    monkeypatch.setattr(
        process_pending,
        "_sync_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            LarkCLIError("permission denied", kind="permission")
        ),
    )
    assert process_pending.main(["sync-feishu", "--link", _article()["link"]]) == 1
    failed = get_processed_entry(_article()["link"])
    assert failed["sync_status"] == "pending"
    assert "permission denied" in failed["sync_error"]
    assert "remains local" in caplog.text

    monkeypatch.setattr(
        process_pending,
        "_sync_entry",
        lambda entry, *, dry_run=False: update_sync_status(
            entry["article"]["link"], "synced"
        ),
    )
    assert process_pending.main(["sync-feishu", "--link", _article()["link"]]) == 0
    assert get_processed_entry(_article()["link"])["sync_status"] == "synced"


def test_bulk_sync_rejects_real_writes(monkeypatch: pytest.MonkeyPatch):
    import process_pending

    monkeypatch.setattr(
        process_pending,
        "_sync_entry",
        lambda *_args, **_kwargs: pytest.fail("bulk mode must not attempt a write"),
    )
    with pytest.raises(ValueError, match="preview-only"):
        process_pending.cmd_sync_all(dry_run=False)
