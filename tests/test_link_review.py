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
from scoring_rubric import SCORING_DIMENSIONS, calculate_score, should_sync  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
