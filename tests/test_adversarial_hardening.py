from __future__ import annotations

import io
import json

import pytest


def _article(suffix: str) -> dict:
    return {
        "title": f"Article {suffix}",
        "account": "Example",
        "digest": "Digest",
        "update_time": 1_700_000_000,
        "link": f"https://mp.weixin.qq.com/s/{suffix}",
    }


def test_direct_lark_data_writes_are_blocked():
    from lark_runtime import safe_lark_arguments

    with pytest.raises(ValueError, match="managed process/manage"):
        safe_lark_arguments(
            [
                "base",
                "+record-upsert",
                "--base-token",
                "bas_secret",
                "--table-id",
                "tbl_x",
                "--json",
                "{}",
                "--yes",
            ]
        )
    with pytest.raises(ValueError, match="managed process/manage"):
        safe_lark_arguments(
            [
                "drive",
                "+member-add",
                "--token",
                "bas_secret",
                "--member-id",
                "ou_x",
                "--perm",
                "full_access",
                "--yes",
            ]
        )


def test_host_context_is_rejected_without_detected_agent_runtime(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    for names in (
        ("OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_GATEWAY_TOKEN"),
        ("HERMES_HOME", "HERMES_STATE_DIR"),
        ("LARK_CHANNEL", "LARK_CHANNEL_HOME", "LARK_CHANNEL_APP_ID"),
    ):
        for name in names:
            monkeypatch.delenv(name, raising=False)
    import manage
    from config_store import DEFAULT_CONFIG, load_config, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["feishu"]["destination"] = "create"
    save_config(config)
    before = load_config()
    monkeypatch.setattr(
        manage.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "source": "lark-channel",
                    "app_id": "cli_forged123",
                    "sender_open_id": "ou_forgedmanager",
                }
            )
        ),
    )

    assert manage.main(["feishu-host-context", "--agent-stdin"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert "detected supported Agent runtime" in result["error"]["message"]
    assert load_config() == before


def test_bot_host_context_is_rejected_for_create_destination(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LARK_CHANNEL", "1")
    import manage
    from config_store import DEFAULT_CONFIG, load_config, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["feishu"]["destination"] = "create"
    save_config(config)
    before = load_config()
    monkeypatch.setattr(
        manage.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "source": "lark-channel",
                    "app_id": "cli_currentbot123",
                    "sender_open_id": "ou_current_sender",
                }
            )
        ),
    )

    assert manage.main(["feishu-host-context", "--agent-stdin"]) == 1
    assert "destination=existing" in json.loads(
        capsys.readouterr().out
    )["error"]["message"]
    assert load_config() == before


def test_legacy_bot_create_state_routes_to_user_identity():
    from config_store import DEFAULT_CONFIG
    from execution_policy import next_stage

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["wechat"] = {"cookie": "cookie", "token": "token"}
    config["health"]["wechat"]["last_verified_at"] = "2026-01-01T00:00:00+00:00"
    config["setup"]["search_window_confirmed"] = True
    config["setup"]["feishu_identity_confirmed"] = True
    config["subscriptions"] = [{"name": "Example", "biz": "biz_example"}]
    config["feishu"].update({"destination": "create", "identity": "bot"})

    assert next_stage(config, cli={"compatible": True}) == (
        "feishu_create_requires_user_identity",
        "switch_to_user_identity",
    )


def test_agent_bot_cannot_create_base_or_grant_manager_access(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LARK_CHANNEL", "1")
    import manage
    from config_store import DEFAULT_CONFIG, load_config, save_config

    save_config(json.loads(json.dumps(DEFAULT_CONFIG)))
    assert manage.main(["feishu-destination", "--mode", "existing"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        manage.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "source": "lark-channel",
                    "app_id": "cli_currentbot123",
                    "sender_open_id": "ou_current_sender",
                }
            )
        ),
    )
    assert manage.main(["feishu-host-context", "--agent-stdin"]) == 0
    capsys.readouterr()
    configured = load_config()["feishu"]
    assert configured["expected_app_id"] == "cli_currentbot123"
    assert configured["manager_open_id"] == ""
    assert configured["manager_access"] == "undecided"
    with pytest.raises(SystemExit):
        manage.build_parser().parse_args(
            ["feishu-manager", "--open-id", "ou_different_user"]
        )
    assert load_config()["feishu"]["manager_open_id"] == ""
    assert manage.main(["feishu-destination", "--mode", "create"]) == 1
    assert "requires user identity" in json.loads(
        capsys.readouterr().out
    )["error"]["message"]
    legacy = load_config()
    legacy["feishu"]["destination"] = "create"
    save_config(legacy)
    monkeypatch.setattr(
        manage,
        "create_standard_base",
        lambda *args, **kwargs: pytest.fail("bot flow must stop before Base creation"),
    )

    assert (
        manage.main(
            [
                "feishu-create-base",
                "--name",
                "公众号文章",
                "--table-name",
                "文章列表",
                "--yes",
            ]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert "bot Base creation is disabled" in result["error"]["message"]
    assert load_config()["feishu"]["enabled"] is False


@pytest.mark.parametrize("manager_access", ["undecided", "declined"])
def test_bot_base_creation_stops_without_approved_manager_access(
    manager_access, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    import manage
    from config_store import DEFAULT_CONFIG, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["setup"]["feishu_identity_confirmed"] = True
    config["feishu"].update(
        {
            "destination": "create",
            "identity": "bot",
            "binding_mode": "agent",
            "agent_source": "lark-channel",
            "expected_app_id": "cli_currentbot123",
            "manager_open_id": "ou_current_sender",
            "manager_access": manager_access,
        }
    )
    save_config(config)
    monkeypatch.setattr(
        manage,
        "create_standard_base",
        lambda *args, **kwargs: pytest.fail("must not create without approval"),
    )

    assert manage.main(
        [
            "feishu-create-base",
            "--name",
            "公众号文章",
            "--table-name",
            "文章列表",
            "--yes",
        ]
    ) == 1
    assert "bot Base creation is disabled" in json.loads(
        capsys.readouterr().out
    )["error"]["message"]


def test_non_agent_bot_cannot_create_or_grant_manager_access(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    import manage
    from config_store import DEFAULT_CONFIG, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["setup"]["feishu_identity_confirmed"] = True
    config["feishu"].update(
        {
            "destination": "create",
            "identity": "bot",
            "binding_mode": "dedicated",
            "expected_app_id": "cli_dedicated123",
            "cli_profile": "wechat-article-cli_dedicated123",
            "manager_open_id": "ou_untrusted_manual",
            "manager_access": "approved",
            "manager_access_base_name": "公众号文章",
            "manager_access_table_name": "文章列表",
        }
    )
    save_config(config)
    assert manage.main(
        [
            "feishu-manager-access",
            "--mode",
            "approve",
            "--base-name",
            "公众号文章",
            "--table-name",
            "文章列表",
        ]
    ) == 1
    assert "requires user identity" in json.loads(
        capsys.readouterr().out
    )["error"]["message"]
    monkeypatch.setattr(
        manage,
        "create_standard_base",
        lambda *args, **kwargs: pytest.fail("non-Agent bot must not create a Base"),
    )

    assert manage.main(
        [
            "feishu-create-base",
            "--name",
            "公众号文章",
            "--table-name",
            "文章列表",
            "--yes",
        ]
    ) == 1
    assert "bot Base creation is disabled" in json.loads(
        capsys.readouterr().out
    )["error"]["message"]


def test_manager_access_approval_is_scoped_to_exact_base_and_table(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    import manage
    from config_store import DEFAULT_CONFIG, load_config, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["setup"]["feishu_identity_confirmed"] = True
    config["feishu"].update(
        {
            "destination": "create",
            "identity": "user",
            "binding_mode": "existing",
            "expected_app_id": "cli_currentbot123",
            "cli_profile": "wechat-article-cli_currentbot123",
        }
    )
    save_config(config)
    assert manage.main(
        [
            "feishu-manager-access",
            "--mode",
            "approve",
            "--base-name",
            "批准的 Base",
            "--table-name",
            "批准的数据表",
        ]
    ) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        manage,
        "create_standard_base",
        lambda *args, **kwargs: pytest.fail("mismatched approval must stop before creation"),
    )
    assert manage.main(
        [
            "feishu-create-base",
            "--name",
            "另一个 Base",
            "--table-name",
            "另一个数据表",
            "--yes",
        ]
    ) == 1
    assert "does not match" in json.loads(capsys.readouterr().out)["error"]["message"]

    assert manage.main(["feishu-destination", "--mode", "existing"]) == 0
    capsys.readouterr()
    saved = load_config()["feishu"]
    assert saved["manager_access"] == "undecided"
    assert saved["manager_access_base_name"] == ""
    assert saved["manager_access_table_name"] == ""


def test_identity_change_clears_stale_agent_manager_context(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    import manage
    from config_store import DEFAULT_CONFIG, load_config, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["setup"]["feishu_identity_confirmed"] = True
    config["feishu"].update(
        {
            "destination": "create",
            "identity": "bot",
            "binding_mode": "agent",
            "agent_source": "lark-channel",
            "expected_app_id": "cli_previousbot123",
            "manager_open_id": "ou_previous_sender",
            "manager_access": "approved",
            "manager_access_base_name": "旧 Base",
            "manager_access_table_name": "旧表",
        }
    )
    save_config(config)

    assert manage.main(["feishu-identity", "--as", "user"]) == 0
    capsys.readouterr()
    saved = load_config()["feishu"]
    assert saved["binding_mode"] == ""
    assert saved["agent_source"] == ""
    assert saved["expected_app_id"] == ""
    assert saved["manager_open_id"] == ""
    assert saved["manager_access"] == "undecided"


def test_user_identity_can_create_exactly_approved_base_without_manager_grant(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    import manage
    import bitable_client
    from config_store import DEFAULT_CONFIG, load_config, save_config

    assert not hasattr(bitable_client, "grant_bot_created_resource")
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["setup"]["feishu_identity_confirmed"] = True
    config["feishu"].update(
        {
            "destination": "create",
            "identity": "user",
            "binding_mode": "existing",
            "expected_app_id": "cli_user123",
            "cli_profile": "wechat-article-cli_user123",
        }
    )
    save_config(config)
    assert manage.main(
        [
            "feishu-manager-access",
            "--mode",
            "approve",
            "--base-name",
            "公众号文章",
            "--table-name",
            "文章列表",
        ]
    ) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        manage, "verify_feishu_identity", lambda *args, **kwargs: {"status": "ready"}
    )
    monkeypatch.setattr(
        manage,
        "create_standard_base",
        lambda *args, **kwargs: {
            "data": {
                "base": {"app_token": "bascn_created"},
                "created_table_id": "tbl_created",
            }
        },
    )
    monkeypatch.setattr(
        manage,
        "preflight_feishu",
        lambda *args, **kwargs: {
            "mapping": {
                "title": {"field_id": "fld_title", "name": "标题", "type": "text"},
                "url": {"field_id": "fld_url", "name": "链接", "type": "url"},
            }
        },
    )

    assert manage.main(
        [
            "feishu-create-base",
            "--name",
            "公众号文章",
            "--table-name",
            "文章列表",
            "--yes",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)["data"]
    assert result["creation_identity"] == "user"
    assert result["separate_manager_grant_performed"] is False
    assert load_config()["feishu"]["enabled"] is True


def test_clean_previews_before_irreversible_delete(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    import process_pending
    import queue_helpers
    from queue_helpers import add_pending, complete_article, read_queue

    add_pending([_article("old")])
    complete_article(_article("old")["link"], {"score": 8})
    real_now = queue_helpers.time.time()
    monkeypatch.setattr(queue_helpers.time, "time", lambda: real_now + 400 * 86400)

    assert process_pending.main(["clean", "--days", "365"]) == 0
    assert "Preview: 1" in capsys.readouterr().out
    assert len(read_queue()["processed"]) == 1

    assert process_pending.main(["clean", "--days", "365", "--yes"]) == 0
    assert "Removed 1" in capsys.readouterr().out
    assert read_queue()["processed"] == {}


def test_batch_read_caps_aggregate_article_content(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    import process_pending
    from queue_helpers import add_pending

    add_pending([_article("one"), _article("two"), _article("three")])
    monkeypatch.setattr(process_pending, "MAX_BATCH_CONTENT_CHARS", 10)
    monkeypatch.setattr(
        process_pending,
        "new_session",
        lambda: type("S", (), {"close": lambda self: None})(),
    )
    monkeypatch.setattr(
        process_pending,
        "fetch_article",
        lambda url, **kwargs: {**_article(url.rsplit("/", 1)[-1]), "text": "abcdefgh"},
    )

    assert process_pending.main(["batch-read", "--limit", "3"]) == 0
    output = capsys.readouterr().out
    assert output.count("abcdefgh") == 1
    assert "ab\n" in output
    assert "Content output truncated" in output


def test_batch_deterministic_failure_is_not_retryable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    import process_pending
    from article_reader import ArticleContentError
    from queue_helpers import add_pending

    add_pending([_article("bad")])
    monkeypatch.setattr(
        process_pending,
        "new_session",
        lambda: type("S", (), {"close": lambda self: None})(),
    )
    monkeypatch.setattr(
        process_pending,
        "fetch_article",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ArticleContentError("invalid article")
        ),
    )

    assert process_pending.main(["--format", "json", "batch-read", "--limit", "1"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "ARTICLE_CONTENT_INVALID"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["details"]["failure_codes"] == ["ARTICLE_CONTENT_INVALID"]
