"""Release-gate tests: version consistency and clean validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import validate_release


def test_plugin_version_matches_changelog():
    validate_release.validate_version_consistency()


def test_plugin_version_drift_fails_validation(tmp_path: Path):
    plugin = tmp_path / "plugin.json"
    changelog = tmp_path / "CHANGELOG.md"
    plugin.write_text(
        json.dumps({"name": "wechat-article-link-reviewer", "version": "1.0.0"}),
        encoding="utf-8",
    )
    changelog.write_text("## 2.1.0 - Unreleased\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        validate_release.validate_version_consistency(plugin, changelog)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("配置微信公众号文章" + "\u8ba2\u9605", "unrelated legacy setup prompt"),
        ("WeChat Article " + "Sub" + "scriber", "different product display name"),
        ("wechat-article-" + "sub" + "scriber", "different product identifier"),
    ],
)
def test_stale_public_product_terms_fail_validation(text: str, reason: str):
    with pytest.raises(ValueError, match=reason):
        validate_release.validate_no_stale_product_terms({"public-file": text})


def test_release_is_dated_and_cross_platform():
    validate_release.validate_release_readiness()


def test_release_validation_passes(tmp_path: Path):
    assert validate_release.main() == 0
