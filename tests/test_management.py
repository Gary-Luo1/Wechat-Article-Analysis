from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "wechat-article-subscriber" / "scripts"


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
    assert "manage" in runtime.COMMANDS
    with pytest.raises(SystemExit):
        process_pending.build_parser().parse_args(["ingest", "--url", _article()["link"]])
    with pytest.raises(SystemExit):
        manage.build_parser().parse_args(["subscriptions", "list"])
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
