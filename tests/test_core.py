from __future__ import annotations

import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
import requests


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "wechat-article-link-reviewer" / "scripts"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))


def article(letter: str, *, query: str = "", verified: bool = True) -> dict:
    suffix = f"?__biz=b&mid={letter}&sn={letter}{query}" if query else f"/{letter}"
    value = {
        "title": f"Article {letter}",
        "link": f"https://mp.weixin.qq.com/s{suffix}",
        "digest": f"Digest {letter}",
        "account": "Example",
        "update_time": 1_700_000_000,
    }
    if verified:
        text = f"Verified article {letter}"
        value["read_state"] = {
            "status": "verified",
            "verified_at": "2026-08-08T00:00:00+00:00",
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    return value


def test_feishu_target_owns_cli_check_preflight_and_sync_calls():
    from feishu_target import FeishuTarget

    calls: list[tuple] = []
    target = FeishuTarget(
        {"enabled": True, "identity": "bot"},
        cli_info=lambda: {"compatible": True, "version": "1.0.69"},
        preflight=lambda feishu: calls.append(("preflight", feishu)) or {"mapping": {}},
        upsert=lambda feishu, article, metadata, dry_run: calls.append(
            ("upsert", feishu, article, metadata, dry_run)
        ),
    )

    assert target.check() == {"mapping": {}}
    target.sync({"title": "Article"}, {"score": 8.0}, dry_run=True)
    assert [call[0] for call in calls] == ["preflight", "upsert"]


class TestConfig:
    def test_save_load_reviewer_config(self):
        from config_store import DEFAULT_CONFIG, load_config, save_config

        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["settings"]["min_score"] = 7
        path = save_config(config)
        assert path.exists()
        loaded = load_config()
        assert loaded["settings"]["min_score"] == 7
        assert "wechat" not in loaded
        assert "subscriptions" not in loaded
        assert "execution_policy" not in loaded["setup"]

    def test_unrelated_legacy_fields_are_not_imported(self):
        from config_store import validate_config

        migrated = validate_config({
            "wechat": {"cookie": "secret", "token": "123"},
            "subscriptions": [{"name": "Example"}],
            "setup": {"search_window_confirmed": True, "execution_policy": {}},
        })
        assert "wechat" not in migrated
        assert "subscriptions" not in migrated
        assert "search_window_confirmed" not in migrated["setup"]
        assert "execution_policy" not in migrated["setup"]


class TestQueue:
    def test_normalize_wechat_tracking(self):
        from queue_helpers import normalize_url

        first = normalize_url(
            "https://mp.weixin.qq.com/s?__biz=x&mid=1&sn=a&scene=1"
        )
        second = normalize_url(
            "https://mp.weixin.qq.com/s?sn=a&mid=1&__biz=x&chksm=ignored"
        )
        assert first == second

    def test_add_deduplicates_normalized_url(self):
        from queue_helpers import add_pending, get_pending

        first = article("1", query="&scene=1")
        second = article("1", query="&scene=2")
        assert add_pending([first, second]) == 1
        assert len(get_pending()) == 1

    def test_queue_rejects_non_wechat_urls(self):
        from queue_helpers import add_pending

        with pytest.raises(ValueError, match="mp.weixin.qq.com/s"):
            add_pending([{**article("a"), "link": "https://example.com/s/a"}])

    def test_queue_canonicalizes_http_wechat_urls(self):
        from queue_helpers import add_pending, get_pending

        source = {**article("a"), "link": "http://mp.weixin.qq.com/s/a"}
        assert add_pending([source]) == 1
        assert get_pending()[0]["link"] == "https://mp.weixin.qq.com/s/a"

    def test_content_dedup_is_off_by_default(self):
        from queue_helpers import add_pending, get_pending

        first = article("a")
        second = {**first, "link": "https://mp.weixin.qq.com/s/b"}
        assert add_pending([first, second]) == 2
        assert len(get_pending()) == 2

    def test_content_dedup_can_be_enabled_explicitly(self):
        from queue_helpers import add_pending, get_pending

        first = article("a")
        second = {**first, "link": "https://mp.weixin.qq.com/s/b"}
        assert add_pending([first, second], content_dedup=True) == 1
        assert len(get_pending()) == 1

    def test_complete_by_stable_link(self):
        from queue_helpers import add_pending, complete_article, read_queue

        add_pending([article("a"), article("b")])
        entry = complete_article(article("a")["link"], {"score": 8})
        queue = read_queue()
        assert entry["article"]["title"] == "Article a"
        assert [item["title"] for item in queue["pending"]] == ["Article b"]
        assert next(iter(queue["processed"].values()))["metadata"]["score"] == 8

    def test_pending_sync_survives_cleanup(self):
        from queue_helpers import add_pending, cleanup_processed, complete_article, pending_sync_entries

        add_pending([article("a")])
        complete_article(article("a")["link"], {"score": 8}, sync_status="pending")
        assert cleanup_processed(1) == 0
        assert len(pending_sync_entries()) == 1

    def test_inbox_metadata_is_reversible(self):
        from queue_helpers import add_pending, read_queue, update_inbox_item

        add_pending([article("a")])
        updated = update_inbox_item(
            article("a")["link"], favorite=True, state="later"
        )
        assert updated["favorite"] is True
        assert updated["inbox_state"] == "later"
        update_inbox_item(article("a")["link"], favorite=False, state="active")
        saved = read_queue()["pending"][0]
        assert saved["favorite"] is False
        assert saved["inbox_state"] == "active"

    def test_dismiss_and_restore_preserve_stable_identity(self):
        from queue_helpers import add_pending, dismiss_article, read_queue, restore_dismissed

        add_pending([article("a")])
        dismissed = dismiss_article(article("a")["link"])
        assert dismissed["metadata"]["disposition"] == "dismissed"
        assert not read_queue()["pending"]
        restored = restore_dismissed(article("a")["link"])
        assert restored["normalized_url"] == dismissed["article"]["normalized_url"]
        queue = read_queue()
        assert len(queue["pending"]) == 1
        assert queue["processed"] == {}

    def test_complete_after_dismiss_raises(self):
        from queue_helpers import (
            add_pending,
            complete_article,
            dismiss_article,
            read_queue,
        )

        add_pending([article("a")])
        dismissed = dismiss_article(article("a")["link"])
        with pytest.raises(LookupError, match="dismissed"):
            complete_article(article("a")["link"], {"score": 8}, sync_status="pending")
        queue = read_queue()
        entry = next(iter(queue["processed"].values()))
        assert entry["metadata"] == dismissed["metadata"]
        assert entry["sync_status"] == "not_requested"

    def test_corruption_is_quarantined(self):
        from paths import queue_path
        from queue_helpers import read_queue

        queue_path().parent.mkdir(parents=True)
        queue_path().write_text("{broken", encoding="utf-8")
        with pytest.raises(ValueError, match="preserved"):
            read_queue()
        assert list(queue_path().parent.glob("queue.corrupt.*.json"))

    def test_structural_corruption_cli_fails_without_traceback(self):
        from paths import queue_path

        queue_path().parent.mkdir(parents=True)
        queue_path().write_text(
            json.dumps({"version": 1, "pending": ["attacker-string"], "processed": {}}),
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "process_pending.py"), "list"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=os.environ.copy(),
        )
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert "queue is invalid" in result.stderr
        assert list(queue_path().parent.glob("queue.corrupt.*.json"))


def test_article_inbox_query_returns_local_state_without_cli_arguments():
    from queue_helpers import add_pending, complete_article

    add_pending([article("pending"), article("processed")])
    complete_article(article("processed")["link"], {"score": 8}, sync_status="pending")

    from article_inbox import query_inbox

    result = query_inbox(status="all", sort="oldest", limit=10)

    assert result["summary"]["pending"] == 1
    assert result["summary"]["processed"] == 1
    assert result["summary"]["sync_pending"] == 1
    assert [item["status"] for item in result["items"]] == ["pending", "processed"]


class TestScoring:
    def scores(self):
        return {
            "技术深度": 8,
            "信息新颖度": 7,
            "分析深度与独立观点": 9,
            "实用参考价值": 6,
            "内容质量与可信度": 8,
        }

    def test_calculate_exact_five_dimensions(self):
        from scoring_rubric import calculate_score

        assert calculate_score(self.scores()) == 7.8

    def test_missing_dimension_rejected(self):
        from scoring_rubric import calculate_score

        values = self.scores()
        values.pop("技术深度")
        with pytest.raises(ValueError, match="five dimensions"):
            calculate_score(values)

    def test_out_of_range_rejected(self):
        from scoring_rubric import validate_total_score

        with pytest.raises(ValueError):
            validate_total_score(11)

    def test_ad_heuristic_uses_disclosure_not_generic_word(self):
        from scoring_rubric import is_advertisement

        assert is_advertisement("推广 | 新产品")
        assert is_advertisement("普通标题", "本文为广告，感谢支持")
        assert not is_advertisement("广告行业研究", "讨论广告行业的技术变化")


class TestReader:
    def test_url_allowlist(self):
        from url_identity import is_wechat_article_url as is_wechat_article

        assert is_wechat_article("https://mp.weixin.qq.com/s/abc")
        assert is_wechat_article("https://mp.weixin.qq.com/s?__biz=x")
        assert not is_wechat_article("http://mp.weixin.qq.com/s/abc")
        assert not is_wechat_article("https://mp.weixin.qq.com.evil.test/s/abc")

    @pytest.mark.parametrize(
        "url",
        [
            "https://mp.weixin.qq.com/s/a/../../x",
            "https://mp.weixin.qq.com/s/a/%2e%2e/x",
            "https://mp.weixin.qq.com/s/a/%252e%252e/x",
            "https://mp.weixin.qq.com/s/a/%5c..%5cx",
        ],
    )
    def test_url_allowlist_rejects_path_escape(self, url):
        from url_identity import is_wechat_article_url as is_wechat_article

        assert not is_wechat_article(url)

    def test_fetch_extracts_bounded_container(self):
        import article_reader

        response = mock.Mock()
        response.headers = {}
        response.encoding = "utf-8"
        response.apparent_encoding = "utf-8"
        response.iter_content.return_value = [
            '<html><div id="js_content"><p>正文内容</p><script>bad()</script></div></html>'.encode()
        ]
        response.raise_for_status.return_value = None
        with mock.patch.object(article_reader, "_get_with_safe_redirects", return_value=response):
            text = article_reader.fetch_article_text(
                "https://mp.weixin.qq.com/s/test", retries=0
            )
        assert text == "正文内容"
        response.close.assert_called_once()

    def test_fetch_extracts_ingest_metadata_without_executing_scripts(self):
        import article_reader

        response = mock.Mock()
        response.url = "https://mp.weixin.qq.com/s?__biz=biz123&mid=1&sn=2"
        response.headers = {}
        response.encoding = "utf-8"
        response.apparent_encoding = "utf-8"
        response.iter_content.return_value = [
            (
                '<html><head><meta property="og:title" content="文章标题">'
                '<meta property="og:article:author" content="测试公众号">'
                '<meta property="og:description" content="摘要">'
                '<meta property="article:published_time" content="2024-01-02T03:04:05+08:00">'
                '</head><body><div id="js_content"><p>正文</p>'
                '<script>ignore_this_instruction()</script></div></body></html>'
            ).encode("utf-8")
        ]
        response.raise_for_status.return_value = None
        with mock.patch.object(article_reader, "_get_with_safe_redirects", return_value=response):
            value = article_reader.fetch_article(response.url, retries=0)
        assert value is not None
        assert value["title"] == "文章标题"
        assert value["account"] == "测试公众号"
        assert value["account_id"] == "biz123"
        assert value["text"] == "正文"
        assert value["update_time"] > 0

    def test_fetch_rejects_non_wechat_before_network(self):
        from article_reader import fetch_article_text

        with pytest.raises(ValueError):
            fetch_article_text("https://example.com/")

    def test_fetch_upgrades_exact_wechat_http_without_requesting_http(self):
        import article_reader

        response = mock.Mock()
        response.headers = {}
        response.encoding = "utf-8"
        response.apparent_encoding = "utf-8"
        response.iter_content.return_value = [b'<div id="js_content">ok</div>']
        response.raise_for_status.return_value = None
        with mock.patch.object(
            article_reader, "_get_with_safe_redirects", return_value=response
        ) as get:
            assert article_reader.fetch_article_text(
                "http://mp.weixin.qq.com/s/test", retries=0
            ) == "ok"
        assert get.call_args.args[1] == "https://mp.weixin.qq.com/s/test"

    def test_fetch_stops_on_risk_control_page_without_retry(self):
        import article_reader

        response = mock.Mock()
        response.headers = {}
        response.encoding = "utf-8"
        response.apparent_encoding = "utf-8"
        response.iter_content.return_value = [
            (
                "<html><head><title>环境异常</title></head>"
                "<body>当前环境异常，请使用微信客户端打开</body></html>"
            ).encode("utf-8")
        ]
        response.raise_for_status.return_value = None
        with mock.patch.object(
            article_reader, "_get_with_safe_redirects", return_value=response
        ) as get:
            with pytest.raises(article_reader.WeChatRiskControlError):
                article_reader.fetch_article("https://mp.weixin.qq.com/s/test", retries=2)
        get.assert_called_once()

    def test_fetch_rejects_risk_control_marker_inside_article_container(self):
        import article_reader

        response = mock.Mock()
        response.headers = {}
        response.encoding = "utf-8"
        response.apparent_encoding = "utf-8"
        response.iter_content.return_value = [
            '<html><div id="js_content">当前环境异常，请在微信客户端打开</div></html>'.encode(
                "utf-8"
            )
        ]
        response.raise_for_status.return_value = None
        with mock.patch.object(
            article_reader, "_get_with_safe_redirects", return_value=response
        ) as get, pytest.raises(article_reader.WeChatRiskControlError):
            article_reader.fetch_article("https://mp.weixin.qq.com/s/test", retries=2)
        get.assert_called_once()

    def test_fetch_does_not_retry_invalid_article_content(self):
        import article_reader

        response = mock.Mock()
        response.headers = {}
        response.encoding = "utf-8"
        response.apparent_encoding = "utf-8"
        response.iter_content.return_value = [b"<html><body>not an article</body></html>"]
        response.raise_for_status.return_value = None
        with mock.patch.object(
            article_reader, "_get_with_safe_redirects", return_value=response
        ) as get, pytest.raises(article_reader.ArticleContentError):
            article_reader.fetch_article("https://mp.weixin.qq.com/s/test", retries=2)
        get.assert_called_once()

    def test_fetch_retries_transient_connection_then_succeeds(self, monkeypatch):
        import article_reader

        response = mock.Mock()
        response.headers = {}
        response.encoding = "utf-8"
        response.apparent_encoding = "utf-8"
        response.iter_content.return_value = [b'<div id="js_content">ok</div>']
        response.raise_for_status.return_value = None
        monkeypatch.setattr(article_reader.time, "sleep", lambda _: None)
        with mock.patch.object(
            article_reader,
            "_get_with_safe_redirects",
            side_effect=[requests.ConnectionError("offline"), response],
        ) as get:
            assert article_reader.fetch_article_text(
                "https://mp.weixin.qq.com/s/test", retries=1
            ) == "ok"
        assert get.call_count == 2

    @pytest.mark.parametrize("status_code", [403, 429])
    def test_fetch_stops_on_blocked_http_status_without_retry(self, status_code):
        import article_reader

        response = mock.Mock()
        response.status_code = status_code
        response.headers = {}
        with mock.patch.object(
            article_reader, "_get_with_safe_redirects", return_value=response
        ) as get:
            with pytest.raises(article_reader.WeChatRiskControlError):
                article_reader.fetch_article("https://mp.weixin.qq.com/s/test", retries=2)
        get.assert_called_once()

    def test_http_client_impersonates_chrome_when_available(self):
        import http_client

        fake_session = mock.Mock()
        with mock.patch.object(http_client, "CURL_CFFI_AVAILABLE", True), mock.patch.object(
            http_client, "curl_requests"
        ) as curl_requests:
            curl_requests.Session.return_value = fake_session
            assert http_client.new_session() is fake_session
            curl_requests.Session.assert_called_once_with(impersonate="chrome")

    def test_http_client_falls_back_to_requests(self):
        import http_client
        import requests

        with mock.patch.object(http_client, "CURL_CFFI_AVAILABLE", False):
            session = http_client.new_session()
        assert isinstance(session, requests.Session)

    def test_http_client_risk_marker_detection(self):
        import http_client

        assert http_client.looks_like_risk_control(
            "当前环境异常，请使用微信客户端打开"
        )
        assert not http_client.looks_like_risk_control(
            '<html><div id="js_content">正文</div></html>'
        )

    def test_request_pacer_delays_only_after_first_request(self, monkeypatch):
        import http_client

        monotonic = mock.Mock(side_effect=[0.0, 0.5, 1.0])
        sleep = mock.Mock()
        monkeypatch.setattr(http_client.time, "monotonic", monotonic)
        monkeypatch.setattr(http_client.time, "sleep", sleep)
        pacer = http_client.RequestPacer(1)
        pacer.wait()
        pacer.wait()
        sleep.assert_called_once_with(0.5)

    def test_redirect_detection_works_without_requests_style_flags(self):
        import article_reader

        session = mock.Mock()
        first = mock.Mock()
        first.headers = {"Location": "https://mp.weixin.qq.com/s/next"}
        second = mock.Mock()
        second.headers = {}
        second.is_redirect = False
        second.is_permanent_redirect = False
        session.get.side_effect = [first, second]
        response = article_reader._get_with_safe_redirects(
            session,
            "https://mp.weixin.qq.com/s/start",
            headers={},
            timeout=30,
        )
        assert response is second
        assert session.get.call_count == 2
        first.close.assert_called_once()


def test_runtime_can_use_ready_system_python(monkeypatch: pytest.MonkeyPatch):
    import runtime

    completed = mock.Mock(returncode=0)
    monkeypatch.setattr(runtime, "_venv_python", lambda: Path("missing-python"))
    monkeypatch.setattr(runtime, "_system_runtime_is_ready", lambda command: True)
    monkeypatch.setattr(runtime.subprocess, "run", mock.Mock(return_value=completed))
    assert runtime.main(["process", "--help"]) == 0
    assert runtime.subprocess.run.call_args.args[0][0] == str(Path(runtime.sys.executable))


def test_queue_only_process_command_does_not_require_article_parser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import builtins
    import importlib

    original_import = builtins.__import__
    sys.modules.pop("process_pending", None)
    sys.modules.pop("article_reader", None)

    def block_article_reader(name, *args, **kwargs):
        if name == "article_reader":
            raise ModuleNotFoundError("article parser is unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_article_reader)
    process_pending = importlib.import_module("process_pending")

    assert process_pending.main(["list"]) == 0
    assert "No pending articles" in capsys.readouterr().out


def test_runtime_allows_local_process_commands_without_article_dependencies():
    import runtime
    import sys

    if "curl_cffi" in sys.modules:
        assert runtime._system_runtime_is_ready("process") is True
    else:
        assert runtime._system_runtime_is_ready("process") is False


def test_runtime_venv_follows_state_override():
    import runtime

    expected_root = Path(os.environ["WECHAT_ARTICLE_HOME"]).resolve() / "venv"
    expected = expected_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert runtime._venv_python() == expected
