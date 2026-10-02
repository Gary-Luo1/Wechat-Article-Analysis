"""Behavioral tests for link review, scoring, and Feishu confirmation state."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / ".agents"
    / "skills"
    / "wechat-article-link-reviewer"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

from article_cache import (  # noqa: E402
    INLINE_CONTENT_CHARS,
    load_article_text,
    store_article_text,
)
from config_store import modify_config  # noqa: E402
from manage import _status, main as manage_main  # noqa: E402
from process_pending import main as process_main  # noqa: E402
from queue_helpers import (  # noqa: E402
    add_pending_with_verified_read,
    export_queue,
    update_sync_status,
)
from scoring_rubric import (  # noqa: E402
    SCORING_DIMENSIONS,
    calculate_score,
    is_advertisement,
    rubric_dimensions,
    should_sync,
)
from url_identity import canonicalize_wechat_article_url  # noqa: E402


HIGH_SCORES = {name: 8 for name in SCORING_DIMENSIONS}
LOW_SCORES = {name: 1 for name in SCORING_DIMENSIONS}


def _article(url: str, *, title: str = "Deep dive") -> dict:
    return {
        "title": title,
        "link": url,
        "digest": "A digest",
        "account": "Example Account",
        "account_id": "example",
        "update_time": 1700000000,
    }


class IsolatedState(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["WECHAT_ARTICLE_HOME"] = self.tmp.name

    def process(self, *args: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = process_main(["--format", "json", *args])
        payload = json.loads(buffer.getvalue())
        return code, payload

    def manage(self, *args: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = manage_main(["--format", "json", *args])
        return code, json.loads(buffer.getvalue())


class ScoringAndUrlTests(unittest.TestCase):
    def test_weighted_score_and_threshold(self) -> None:
        self.assertEqual(calculate_score(HIGH_SCORES), 8.0)
        self.assertTrue(should_sync(8, 6))
        self.assertFalse(should_sync(5.9, 6))
        missing = dict(HIGH_SCORES)
        del missing["技术深度"]
        with self.assertRaises(ValueError):
            calculate_score(missing)

    def test_content_ops_rubric_and_explicit_ad_disclosure(self) -> None:
        ops = {name: 8 for name in rubric_dimensions("content_ops")}
        self.assertEqual(calculate_score(ops, rubric="content_ops"), 8.0)
        with self.assertRaises(ValueError):
            calculate_score(HIGH_SCORES, rubric="content_ops")
        self.assertFalse(
            is_advertisement("合规通知", "平台要求推销内容显著标明广告内容。")
        )
        self.assertTrue(is_advertisement("一篇观察", "本文为广告，介绍一款工具。"))
        self.assertTrue(is_advertisement("【广告】新品发布", "正文"))

    def test_http_article_url_is_canonicalized(self) -> None:
        self.assertEqual(
            canonicalize_wechat_article_url("http://mp.weixin.qq.com/s/abc"),
            "https://mp.weixin.qq.com/s/abc",
        )
        with self.assertRaises(ValueError):
            canonicalize_wechat_article_url("https://example.com/s/abc")


class QueuePolicyTests(IsolatedState):
    def test_content_dedup_is_opt_in(self) -> None:
        first = "https://mp.weixin.qq.com/s/dedup-one"
        second = "https://mp.weixin.qq.com/s/dedup-two"
        text = "same body"
        self.assertEqual(
            add_pending_with_verified_read(_article(first), text, content_dedup=True)[0],
            "queued",
        )
        self.assertEqual(
            add_pending_with_verified_read(
                _article(second), text, content_dedup=False
            )[0],
            "queued",
        )
        third = "https://mp.weixin.qq.com/s/dedup-three"
        self.assertEqual(
            add_pending_with_verified_read(_article(third), text, content_dedup=True)[0],
            "duplicate_content",
        )


class StatusTests(IsolatedState):
    def test_missing_config_does_not_block_review(self) -> None:
        data, action = _status()
        self.assertEqual(action, "provide_article_link")
        self.assertTrue(data["config_valid"])
        self.assertFalse(data["review"]["ask_before_fetch"])
        self.assertTrue(data["review"]["fetch_before_setup"])
        self.assertEqual(data["feishu"]["destination"], "undecided")
        self.assertEqual(data["review"]["after_scoring"], "ask_whether_to_configure_feishu")
        self.assertEqual(data["feishu"]["setup_next_action"], "none")

    def test_skip_stays_local_and_ready_target_asks_after_scoring(self) -> None:
        code, payload = self.manage("feishu-destination", "--mode", "skip")
        self.assertEqual(code, 0)
        self.assertEqual(payload["next_action"], "provide_article_link")
        data, action = _status()
        self.assertEqual(action, "provide_article_link")
        self.assertEqual(data["review"]["after_scoring"], "complete_locally")

        def mutate(config: dict) -> dict:
            config["feishu"].update(
                {
                    "destination": "existing",
                    "enabled": True,
                    "identity": "user",
                    "base_token": "bascnTEST",
                    "table_id": "tblTEST",
                    "provisioning": "existing",
                }
            )
            config["setup"]["feishu_identity_confirmed"] = True
            return config

        modify_config(mutate)
        data, action = _status()
        self.assertEqual(action, "provide_article_link")
        self.assertTrue(data["feishu"]["ready_to_write"])
        self.assertEqual(data["review"]["after_scoring"], "ask_write_confirmation")
        self.assertFalse(data["review"]["ask_before_fetch"])

    def test_settings_round_trip(self) -> None:
        code, payload = self.manage(
            "settings", "set", "--min-score", "8.5", "--content-dedup", "on"
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["settings"]["min_score"], 8.5)
        self.assertTrue(payload["data"]["settings"]["content_dedup"])
        data, _action = _status()
        self.assertEqual(data["settings"]["min_score"], 8.5)
        self.assertTrue(data["settings"]["content_dedup"])
        code, failed = self.manage("settings", "set", "--min-score", "0")
        self.assertEqual(code, 1)
        self.assertEqual(failed["error"]["code"], "CONFIG_ERROR")
        code, payload = self.manage("settings", "set", "--rubric", "content_ops")
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["settings"]["rubric"], "content_ops")
        data, _action = _status()
        self.assertEqual(data["settings"]["rubric"], "content_ops")
        self.assertEqual(
            [item["name"] for item in data["settings"]["dimensions"]],
            list(rubric_dimensions("content_ops")),
        )


class ReviewFlowTests(IsolatedState):
    def _queue(self, url: str, text: str, *, title: str = "Deep dive") -> None:
        add_pending_with_verified_read(_article(url, title=title), text)
        store_article_text(url, text)

    def test_done_is_structured_and_routes_processed_writes(self) -> None:
        url = "https://mp.weixin.qq.com/s/done-local"
        text = "Verified article body for the local completion path."
        self._queue(url, text)
        code, payload = self.process(
            "done",
            "--link",
            url,
            "--dims",
            json.dumps(HIGH_SCORES),
            "--summary",
            "Useful.",
            "--tags",
            "ai",
        )
        self.assertEqual(code, 0)
        data = payload["data"]
        self.assertTrue(payload["ok"])
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["sync_status"], "not_requested")
        self.assertFalse(data["feishu_written"])
        self.assertEqual(data["score"], 8.0)
        self.assertEqual(data["document_url"], "")
        self.assertIsNone(load_article_text(url))

        code, failed = self.process("done", "--link", url, "--feishu")
        self.assertEqual(code, 1)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"]["code"], "CONFIG_ERROR")
        self.assertNotIn("sync-feishu --link", failed["error"]["message"])

    def test_below_threshold_write_needs_force_but_outbox_retry_does_not(self) -> None:
        url = "https://mp.weixin.qq.com/s/below-threshold"
        self._queue(url, "Short low-score body.")
        code, payload = self.process(
            "done",
            "--link",
            url,
            "--feishu",
            "--dims",
            json.dumps(LOW_SCORES),
            "--summary",
            "Thin.",
            "--tags",
            "news",
        )
        self.assertEqual(code, 0)
        data = payload["data"]
        self.assertEqual(data["status"], "below_threshold")
        self.assertEqual(data["sync_status"], "skipped_low_score")
        self.assertFalse(data["feishu_written"])
        self.assertEqual(payload["next_action"], "confirm_below_threshold_write")

        update_sync_status(url, "pending")
        code, failed = self.process("done", "--link", url, "--feishu")
        self.assertEqual(code, 1)
        self.assertEqual(failed["error"]["code"], "CONFIG_ERROR")
        self.assertNotIn("threshold", failed["error"]["message"])

    def test_evaluate_reuses_cache_and_bounds_the_tool_result(self) -> None:
        url = "https://mp.weixin.qq.com/s/cached-article"
        text = "cached body"
        self._queue(url, text)
        with mock.patch("process_pending.fetch_article", side_effect=AssertionError("network")):
            code, payload = self.process("evaluate", "--url", url)
        self.assertEqual(code, 0)
        data = payload["data"]
        self.assertEqual(data["status"], "already_pending")
        self.assertTrue(data["content_from_cache"])
        self.assertFalse(data["content_truncated"])
        self.assertEqual(data["untrusted_article_content"], text)
        self.assertEqual(payload["next_action"], "score_and_complete_by_url")

        long_url = "https://mp.weixin.qq.com/s/long-article"
        long_text = "长" * (INLINE_CONTENT_CHARS + 40)
        self._queue(long_url, long_text, title="【广告】活动")
        code, payload = self.process("evaluate", "--url", long_url)
        self.assertEqual(code, 0)
        data = payload["data"]
        self.assertTrue(data["content_truncated"])
        self.assertTrue(data["ad_heuristic"])
        self.assertEqual(payload["next_action"], "confirm_advertisement")
        self.assertLess(len(data["untrusted_article_content"]), len(long_text))

        plain_url = "https://mp.weixin.qq.com/s/long-plain"
        self._queue(plain_url, long_text, title="Architecture notes")
        code, payload = self.process("evaluate", "--url", plain_url)
        self.assertEqual(payload["next_action"], "read_cached_article_before_scoring")
        code, full = self.process("content", "--link", plain_url)
        self.assertEqual(code, 0)
        self.assertEqual(full["data"]["content_chars"], len(long_text))
        self.assertFalse(full["data"]["content_truncated"])
        self.assertEqual(full["data"]["untrusted_article_content"], long_text)

        exported = Path(self.tmp.name) / "export.json"
        export_queue(exported)
        exported_text = exported.read_text(encoding="utf-8")
        self.assertNotIn(long_text, exported_text)
        self.assertNotIn("cached body", exported_text)


class FeishuSetupTests(IsolatedState):
    def test_first_app_keeps_a_bound_table(self) -> None:
        self.manage("feishu-identity", "--as", "user")

        def mutate(config: dict) -> dict:
            config["feishu"].update(
                {
                    "destination": "existing",
                    "enabled": True,
                    "base_token": "bascnKEEP",
                    "table_id": "tblKEEP",
                    "base_url": "https://my.feishu.cn/base/bascnKEEP?table=tblKEEP",
                }
            )
            return config

        modify_config(mutate)
        code, payload = self.manage("feishu-app", "--app-id", "cli_aaaaaaaa")
        self.assertEqual(code, 0)
        self.assertFalse(payload["data"]["target_cleared"])
        self.assertEqual(payload["next_action"], "reuse_or_configure_private_lark_profile")
        from config_store import load_config

        self.assertEqual(load_config()["feishu"]["table_id"], "tblKEEP")
        code, switched = self.manage("feishu-app", "--app-id", "cli_bbbbbbbb")
        self.assertEqual(code, 0)
        self.assertTrue(switched["data"]["target_cleared"])
        self.assertEqual(switched["next_action"], "rebind_feishu_table")
        self.assertEqual(load_config()["feishu"]["table_id"], "")

    def test_app_init_preview_does_not_create_an_app(self) -> None:
        code, payload = self.manage("feishu-app-init")
        self.assertEqual(code, 0)
        self.assertEqual(payload["next_action"], "rerun_with_yes")
        self.assertFalse(payload["data"]["existing_app_present"])
        code, status = self.manage("feishu-app-init", "status")
        self.assertEqual(code, 0)
        self.assertEqual(status["next_action"], "run_feishu_app_init")

        config_dir = Path(self.tmp.name) / "lark-cli-home" / ".lark-cli"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text(
            json.dumps(
                {
                    "apps": [
                        {
                            "name": "cli_aaaaaaaa",
                            "appId": "cli_aaaaaaaa",
                            "appSecret": {"source": "keychain", "id": "appsecret:cli_aaaaaaaa"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        code, existing = self.manage("feishu-app-init", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(existing["next_action"], "select_existing_feishu_app")
        self.assertTrue(existing["data"]["existing_app_present"])
        self.assertFalse(existing["data"]["app_id_included"])

    def test_record_url_and_init_output_parser(self) -> None:
        from bitable_client import feishu_record_url
        from lark_runtime import parse_feishu_app_init_output

        url = feishu_record_url(
            {
                "base_url": "https://my.feishu.cn/base/abc?table=tbl123",
                "table_id": "tbl123",
            },
            "recAb12",
        )
        self.assertIn("table=tbl123", url)
        self.assertIn("record=recAb12", url)
        self.assertEqual(
            feishu_record_url(
                {"base_url": "https://my.feishu.cn/base/abc", "table_id": "tbl123"},
                "not-a-record",
            ),
            "",
        )
        parsed = parse_feishu_app_init_output(
            "打开 https://open.feishu.cn/page/cli?user_code=ABCD-EFGH\n"
            "OK: 应用配置成功! App ID: cli_abc123\n"
        )
        self.assertTrue(parsed["succeeded"])
        self.assertEqual(parsed["app_id"], "cli_abc123")
        self.assertTrue(str(parsed["verification_url"]).startswith("https://open.feishu.cn/"))

    @unittest.skipIf(os.name == "nt", "the stand-in lark-cli is a POSIX shell script")
    def test_skill_owned_home_can_receive_a_login_write(self) -> None:
        home = Path(self.tmp.name) / "lark-cli-home"
        config_dir = home / ".lark-cli"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text('{"apps":[]}\n', encoding="utf-8")
        cli = Path(self.tmp.name) / "fake-lark"
        cli.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' '{\"ok\":true}'\n"
            "printf '%s\\n' '{\"apps\":[{\"name\":\"touched\"}]}' > \"$HOME/.lark-cli/config.json\"\n",
            encoding="utf-8",
        )
        cli.chmod(0o755)
        previous_home = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        os.environ["WECHAT_LARK_CLI_PATH"] = str(cli)

        def restore() -> None:
            os.environ.pop("WECHAT_LARK_CLI_PATH", None)
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home

        self.addCleanup(restore)
        from bitable_client import _run_lark

        payload = _run_lark(["auth", "status", "--json"], retries=1)
        self.assertTrue(payload["ok"])

    def test_done_scores_with_the_selected_rubric(self) -> None:
        self.manage("settings", "set", "--rubric", "content_ops")
        url = "https://mp.weixin.qq.com/s/ops-score"
        add_pending_with_verified_read(_article(url), "运营正文")
        store_article_text(url, "运营正文")
        ops = {name: 6 for name in rubric_dimensions("content_ops")}
        code, payload = self.process(
            "done",
            "--link",
            url,
            "--dims",
            json.dumps(ops, ensure_ascii=False),
            "--summary",
            "清楚。",
            "--tags",
            "运营",
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["score"], 6.0)
        self.assertFalse(payload["data"]["feishu_written"])


if __name__ == "__main__":
    unittest.main()
