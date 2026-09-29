#!/usr/bin/env python3
"""Inspect, patch, diagnose, and safely reset skill state."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from article_cache import article_cache_dir, clear_article_cache
from article_inbox import queue_summary
from bitable_client import (
    LarkCLIError,
    complete_user_device_login,
    create_standard_base,
    created_base_document_url,
    created_base_identifiers,
    device_login_is_expired,
    device_login_is_pending,
    feishu_document_url,
    feishu_identity_context,
    list_fields,
    preflight_feishu,
    resolve_lark_profile,
    standard_field_schema,
    start_user_device_login,
    verify_feishu_identity,
    write_verification_qr,
)
from config_store import (
    DEFAULT_CONFIG,
    ConfigError,
    load_config,
    modify_config,
    redacted_config,
    update_health,
    validate_config,
)
from feishu_target import production_feishu_target
from paths import (
    APP_NAME,
    config_path,
    data_dir,
    lock_path,
    queue_path,
    secure_write_json,
    venv_dir,
)
from lark_runtime import (
    discover_global_lark_profiles,
    import_global_lark_profile,
    isolated_cli_environment,
    isolated_lark_apps,
    lark_cli_work_dir,
    parse_feishu_app_init_output,
    profile_name_for_app,
    resolve_lark_cli,
)
from scoring_rubric import RUBRIC_TECHNICAL, public_rubric
from protocol import dump, failure, success


def _target_configured(feishu: dict[str, Any]) -> bool:
    return bool(str(feishu.get("base_token") or "").strip()) and bool(
        str(feishu.get("table_id") or "").strip()
    )


def _setup_next_action(config: dict[str, Any]) -> str:
    """Return the next Feishu setup command for an in-progress destination."""
    feishu = config["feishu"]
    if not config["setup"]["feishu_identity_confirmed"]:
        if config["setup"]["feishu_authorization"].get("state") == "waiting":
            return "open_verification_url_then_complete_feishu_auth"
        return "run_feishu_auth_start"
    destination = str(feishu.get("destination") or "")
    target = _target_configured(feishu)
    if destination == "existing" and not target:
        return "configure_existing_feishu_target"
    if destination == "create":
        if feishu.get("manager_access") != "approved":
            return "ask_feishu_manager_access"
        if feishu.get("provisioning") != "created" or not target:
            return "preview_feishu_base_creation"
    if not target or not feishu.get("enabled"):
        return "finish_feishu_setup_before_write"
    return "authorize_and_run_feishu_check"


def review_guidance(config: dict[str, Any] | None) -> dict[str, Any]:
    """Describe persisted Feishu state without blocking article reading."""
    if config is None:
        return {
            "destination": "undecided",
            "enabled": False,
            "target_configured": False,
            "identity": "user",
            "identity_confirmed": False,
            "authorization_state": "not_started",
            "manager_access": "undecided",
            "ready_to_write": False,
            "document_url_configured": False,
            "after_scoring": "ask_whether_to_configure_feishu",
            "setup_next_action": "none",
            "min_score": DEFAULT_CONFIG["settings"]["min_score"],
            "content_dedup": DEFAULT_CONFIG["settings"]["content_dedup"],
            "rubric": DEFAULT_CONFIG["settings"]["rubric"],
        }
    feishu = config["feishu"]
    destination = str(feishu.get("destination") or "undecided")
    target = _target_configured(feishu)
    identity_confirmed = bool(config["setup"]["feishu_identity_confirmed"])
    ready = (
        destination in {"existing", "create"}
        and bool(feishu.get("enabled"))
        and target
        and identity_confirmed
    )
    if destination == "skip":
        after_scoring = "complete_locally"
        setup_next_action = "none"
    elif ready:
        after_scoring = "ask_write_confirmation"
        setup_next_action = "none"
    elif destination == "undecided":
        after_scoring = "ask_whether_to_configure_feishu"
        setup_next_action = "none"
    else:
        after_scoring = "finish_feishu_setup_before_write"
        setup_next_action = _setup_next_action(config)
    return {
        "destination": destination,
        "enabled": bool(feishu.get("enabled")),
        "target_configured": target,
        "identity": feishu.get("identity"),
        "identity_confirmed": identity_confirmed,
        "authorization_state": config["setup"]["feishu_authorization"].get("state"),
        "manager_access": feishu.get("manager_access"),
        "ready_to_write": ready,
        "document_url_configured": bool(str(feishu.get("base_url") or "").strip()),
        "after_scoring": after_scoring,
        "setup_next_action": setup_next_action,
        "min_score": config["settings"]["min_score"],
        "content_dedup": config["settings"]["content_dedup"],
        "rubric": str(config["settings"].get("rubric") or RUBRIC_TECHNICAL),
    }


def _review_policy(guidance: dict[str, Any]) -> dict[str, Any]:
    return {
        "fetch_before_setup": True,
        "ask_before_fetch": False,
        "after_scoring": guidance["after_scoring"],
    }


def _authorization(config: dict[str, Any]) -> dict[str, Any]:
    return config["setup"]["feishu_authorization"]


def _public_authorization(authorization: dict[str, Any]) -> dict[str, Any]:
    visible = dict(authorization)
    visible.pop("device_code", None)
    return visible


def waiting_login_is_resumable(authorization: dict[str, Any]) -> bool:
    return (
        authorization.get("state") == "waiting"
        and str(authorization.get("verification_url") or "").strip().startswith("https://")
        and bool(str(authorization.get("device_code") or "").strip())
    )


def created_target_is_resumable(feishu: dict[str, Any]) -> bool:
    return (
        feishu.get("provisioning") == "created"
        and bool(str(feishu.get("base_token") or "").strip())
        and bool(str(feishu.get("table_id") or "").strip())
    )


def complete_authorization_action(
    *, identity_ready: bool, error: LarkCLIError | None
) -> str:
    if error is not None:
        if device_login_is_expired(error):
            return "expired"
        if device_login_is_pending(error) or error.retryable:
            return "keep_waiting"
        return "raise"
    return "authorized" if identity_ready else "keep_waiting"


def _set_manager_access(
    config: dict[str, Any],
    access: str,
    *,
    base_name: str = "",
    table_name: str = "",
) -> None:
    config["feishu"].update(
        {
            "manager_access": access,
            "manager_access_base_name": base_name,
            "manager_access_table_name": table_name,
        }
    )


def _reset_manager_access(config: dict[str, Any]) -> None:
    _set_manager_access(config, "undecided")


def _reset_authorization(config: dict[str, Any], identity: str) -> None:
    state = "not_required" if identity == "bot" else "not_started"
    config["setup"]["feishu_authorization"] = {
        **dict(DEFAULT_CONFIG["setup"]["feishu_authorization"]),
        "state": state,
        "identity": identity,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _doctor(*, online: bool) -> tuple[dict[str, Any], str]:
    """Diagnose only the public-link runtime and optional Feishu target."""
    report: dict[str, Any] = {
        "runtime": {
            "python": platform.python_version(),
            "supported": sys.version_info >= (3, 10),
            "dependencies": {
                name: importlib.util.find_spec(name) is not None
                for name in ("requests", "bs4", "curl_cffi")
            },
        },
        "paths": {
            "data_dir": str(data_dir()),
            "config": str(config_path()),
            "queue": str(queue_path()),
            "venv": str(venv_dir()),
        },
        "mode": "user_supplied_link_only",
    }
    try:
        config = load_config()
    except ConfigError:
        config = None
        exists = config_path().exists()
        # A missing file uses defaults and can still review. A present file
        # that fails validation must be repaired before its Feishu state is trusted.
        report["config"] = {"exists": exists, "valid": not exists}
    else:
        report["config"] = {"exists": True, "valid": True, **redacted_config(config)}

    summary = queue_summary()
    report["queue"] = {"total": summary["pending"] + summary["processed"], **summary}
    if online and config is not None and config["feishu"]["enabled"]:
        try:
            result = production_feishu_target(config["feishu"]).check()
            update_health("feishu", success=True)
            report["online"] = {"feishu": {"ok": True, "preflight": result}}
        except Exception as exc:
            update_health("feishu", success=False, failure_kind=getattr(exc, "kind", type(exc).__name__))
            report["online"] = {"feishu": failure(exc)["error"]}
    guidance = review_guidance(config)
    report["feishu"] = guidance
    report["review"] = _review_policy(guidance)
    report["setup_stage"] = guidance["after_scoring"]
    if config is None and config_path().exists():
        return report, "prepare_or_validate_local_config"
    return report, "provide_article_link"


def _status() -> tuple[dict[str, Any], str]:
    report, next_action = _doctor(online=False)
    guidance = report["feishu"]
    return {
        "mode": report["mode"],
        "config_valid": bool(report.get("config", {}).get("valid")),
        "queue": report["queue"],
        "feishu": guidance,
        "review": report["review"],
        "settings": {
            "min_score": guidance["min_score"],
            "content_dedup": guidance["content_dedup"],
            **public_rubric(str(guidance.get("rubric") or RUBRIC_TECHNICAL)),
        },
        "feishu_configured": bool(guidance.get("enabled")),
    }, next_action


def _expected_app_id(config: dict[str, Any]) -> str:
    """Return the saved Feishu App ID, normalized."""
    return str(config["feishu"].get("expected_app_id") or "").strip()


AGENT_SOURCE_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("openclaw", ("OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_GATEWAY_TOKEN")),
    ("hermes", ("HERMES_HOME", "HERMES_STATE_DIR")),
    ("lark-channel", ("LARK_CHANNEL", "LARK_CHANNEL_HOME", "LARK_CHANNEL_APP_ID")),
)


def _detect_agent_source() -> str:
    """Return the hosting Agent platform from its environment signals."""
    for source, names in AGENT_SOURCE_SIGNALS:
        if any(os.environ.get(name) for name in names):
            return source
    return ""


def _feishu_destination(destination: str) -> tuple[dict[str, Any], str]:
    state: dict[str, Any] = {}

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        if (
            destination == "create"
            and config["setup"]["feishu_identity_confirmed"]
            and config["feishu"]["identity"] == "bot"
        ):
            raise ValueError(
                "new Base creation requires user identity; switch identity before "
                "choosing destination=create"
            )
        previous = str(config["feishu"].get("destination") or "undecided")
        config["feishu"]["destination"] = destination
        if destination == "skip":
            config["feishu"]["enabled"] = False
        state["previous"] = previous
        state["changed"] = previous != destination
        if state["changed"]:
            _reset_manager_access(config)
        return config

    modify_config(mutate)
    next_action = (
        "provide_article_link"
        if destination == "skip"
        else "run_feishu_context_then_authorize_only_if_needed"
    )
    return {
        "destination": destination,
        "previous_destination": state["previous"],
        "explicit_user_choice_required": True,
        "target_or_credentials_deleted": False,
    }, next_action


def _parse_feishu_target_url(value: str) -> tuple[str, str, str]:
    raw = value.strip()
    if not raw or len(raw) > 4096:
        raise ValueError("provide one bounded Feishu Base table URL")
    parsed = urlparse(raw)
    host = str(parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or not host.endswith((".feishu.cn", ".larksuite.com"))
    ):
        raise ValueError("Feishu Base URL must use HTTPS on a feishu.cn or larksuite.com host")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 2 or segments[0] != "base":
        raise ValueError("Feishu Base URL must contain /base/<BASE_TOKEN>")
    base_token = segments[1].strip()
    tables = [item.strip() for item in parse_qs(parsed.query).get("table", []) if item.strip()]
    if len(tables) != 1:
        raise ValueError("Feishu Base URL must identify exactly one table")
    table_id = tables[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,128}", base_token):
        raise ValueError("Feishu Base URL contains an invalid Base token")
    if not re.fullmatch(r"tbl[A-Za-z0-9_-]{3,125}", table_id):
        raise ValueError("Feishu Base URL contains an invalid table ID")
    return base_token, table_id, host


def _feishu_target(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if sys.stdin.isatty():
        raise ValueError("feishu-target --url-stdin requires a Base table URL on stdin")
    raw = sys.stdin.read(4097)
    base_token, table_id, host = _parse_feishu_target_url(raw)
    current = load_config()
    if current["feishu"]["destination"] != "existing":
        raise ValueError("choose destination=existing before configuring a Base table URL")
    changed = (
        current["feishu"].get("base_token") != base_token
        or current["feishu"].get("table_id") != table_id
    )
    preview = {
        "destination": "existing",
        "host": host,
        "target_changed": changed,
        "resource_tokens_included": False,
        "next_check": "process feishu-check --save-mapping",
    }
    if not arguments.yes:
        return {"preview": preview, "configured": False}, "rerun_with_yes"

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        previous = deepcopy(config["feishu"])
        same_target = (
            previous.get("base_token") == base_token
            and previous.get("table_id") == table_id
        )
        config["feishu"].update(
            {
                "destination": "existing",
                "enabled": True,
                "base_token": base_token,
                "table_id": table_id,
                "base_url": raw.strip(),
                "provisioning": "existing",
                "created_base_name": "",
                "created_table_name": "",
            }
        )
        if not same_target:
            config["feishu"]["field_mapping"] = {}
            config["health"]["feishu"] = dict(DEFAULT_CONFIG["health"]["feishu"])
        return config

    modify_config(mutate)
    bound = load_config()["feishu"]
    return {
        **preview,
        "configured": True,
        "document_url": feishu_document_url(bound),
        "resource_tokens_included": False,
    }, "authorize_and_run_feishu_check"


def _import_feishu_host_context(
    arguments: argparse.Namespace,
) -> tuple[dict[str, Any], str]:
    agent_file = getattr(arguments, "agent_file", None)
    if agent_file is not None:
        raw = Path(agent_file).read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            raise ValueError(
                "feishu-host-context --agent-stdin requires trusted host context JSON on stdin"
            )
        raw = sys.stdin.read(16 * 1024 + 1)
    if len(raw.encode("utf-8")) > 16 * 1024:
        raise ValueError("Feishu host context exceeds the input size limit")
    payload = json.loads(raw.lstrip("\ufeff"))
    if not isinstance(payload, dict):
        raise ValueError("Feishu host context must be a JSON object")
    unexpected = set(payload) - {"source", "app_id", "sender_open_id", "sender_id"}
    if unexpected:
        raise ValueError(
            f"Feishu host context contains unsupported keys: {sorted(unexpected)}"
        )
    source = str(payload.get("source") or "").strip().casefold()
    if source not in {"openclaw", "hermes", "lark-channel"}:
        raise ValueError(
            "Feishu host context source must be openclaw, hermes, or lark-channel"
        )
    detected_source = _detect_agent_source()
    if not detected_source:
        raise ValueError(
            "trusted Feishu host context requires a detected supported Agent runtime"
        )
    if detected_source != source:
        raise ValueError(
            "Feishu host context source conflicts with the detected Agent runtime"
        )
    app_id = str(payload.get("app_id") or "").strip()
    if not app_id.startswith("cli_"):
        raise ValueError("trusted Feishu host App ID must start with cli_")
    sender_open_id = str(
        payload.get("sender_open_id") or payload.get("sender_id") or ""
    ).strip()
    if not sender_open_id.startswith("ou_"):
        raise ValueError("trusted Feishu host sender Open ID must start with ou_")

    state: dict[str, Any] = {}

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        destination = config["feishu"]["destination"]
        if destination != "existing":
            raise ValueError(
                "choose destination=existing before importing current Bot host context"
            )
        if (
            config["setup"]["feishu_identity_confirmed"]
            and config["feishu"]["identity"] != "bot"
        ):
            raise ValueError(
                "the current setup already confirms user identity; do not silently switch "
                "it to the conversational bot"
            )
        expected_app_id = _expected_app_id(config)
        if expected_app_id and expected_app_id != app_id:
            raise ValueError(
                "the current Feishu conversation App ID conflicts with the saved App ID"
            )
        previous_scope = (
            config["feishu"]["identity"],
            config["feishu"]["binding_mode"],
            config["feishu"]["agent_source"],
            config["feishu"]["expected_app_id"],
            config["feishu"]["manager_open_id"],
            config["feishu"]["manager_access"],
        )
        same_trusted_context = (
            config["feishu"]["identity"] == "bot"
            and config["feishu"]["binding_mode"] == "agent"
            and config["feishu"]["agent_source"] == source
            and config["feishu"]["expected_app_id"] == app_id
        )
        config["feishu"].update(
            {
                "identity": "bot",
                "binding_mode": "agent",
                "agent_source": source,
                "expected_app_id": app_id,
                "cli_profile": "",
                "expected_user_open_id": "",
                "manager_open_id": "",
            }
        )
        if not same_trusted_context:
            _reset_manager_access(config)
        config["setup"]["feishu_identity_confirmed"] = True
        _reset_authorization(config, "bot")
        current_scope = (
            config["feishu"]["identity"],
            config["feishu"]["binding_mode"],
            config["feishu"]["agent_source"],
            config["feishu"]["expected_app_id"],
            config["feishu"]["manager_open_id"],
            config["feishu"]["manager_access"],
        )
        state["changed"] = previous_scope != current_scope
        if state["changed"]:
            config["health"]["feishu"] = dict(DEFAULT_CONFIG["health"]["feishu"])
        return config

    modify_config(mutate)
    return {
        "source": source,
        "app_id": app_id,
        "identity": "bot",
        "identity_confirmed": True,
        "manager_configured_from_sender": False,
        "sender_persisted": False,
        "sender_open_id_included": False,
        "binding_mode": "agent",
        "host_context_contains_secrets": False,
    }, "bind_detected_feishu_bot"


def _feishu_context(*, verify: bool) -> tuple[dict[str, Any], str]:
    current = load_config()
    if not current["setup"]["feishu_identity_confirmed"]:
        source = _detect_agent_source()
        if source:
            return {
                "identity_required": False,
                "host_bot_context_available": True,
                "agent_source_detected": source,
                "import_command": "manage feishu-host-context --agent-stdin",
                "required_host_fields": ["source", "app_id", "sender_open_id"],
                "rule": (
                    "Read these exact values from the trusted current Feishu host/event "
                    "context. Do not ask the user to type them and do not infer them from "
                    "a display name."
                ),
            }, "import_current_feishu_bot_context"
        return {
            "identity_required": True,
            "choices": {
                "user": (
                    "Use the selected Feishu user's permissions. Reuse a valid existing "
                    "authorization; otherwise start exactly one Base authorization flow."
                ),
                "bot": (
                    "Use app/bot credentials and backend scopes. Never start user authorization."
                ),
            },
            "selection_command": "manage feishu-identity --as user|bot",
        }, "ask_feishu_identity_before_authorization"
    if (
        current["feishu"].get("binding_mode") != "agent"
        and (
            not current["feishu"].get("expected_app_id")
            or not current["feishu"].get("cli_profile")
        )
    ):
        return {
            "identity_required": False,
            "selected_identity": current["feishu"]["identity"],
            "app_selection_required": True,
            "global_profiles_read": False,
            "command": "manage feishu-app --app-id <APP_ID>",
            "rule": (
                "Select the exact App ID first. The Skill creates a private named "
                "profile and never switches or edits global lark-cli profiles."
            ),
        }, "select_feishu_app"
    agent_binding = current["feishu"].get("binding_mode") == "agent"
    expected_app_id = _expected_app_id(current)
    if agent_binding and not expected_app_id:
        return {
            "identity_required": False,
            "host_bot_context_required": True,
            "global_profiles_read": False,
            "default_profile_allowed": False,
            "command": "manage feishu-host-context --agent-stdin",
            "rule": (
                "Import the exact App ID from the trusted current Feishu event "
                "context. Never infer it from the active/default lark-cli profile."
            ),
        }, "import_current_feishu_bot_context"

    profile_resolution = None
    if expected_app_id:
        try:
            profile_resolution = resolve_lark_profile(expected_app_id)
        except LarkCLIError:
            if agent_binding:
                raise

    if (
        profile_resolution
        and current["feishu"].get("cli_profile") != profile_resolution["profile"]
    ):
        resolved_profile = str(profile_resolution["profile"])

        def _set_profile(config: dict[str, Any]) -> dict[str, Any]:
            config["feishu"]["cli_profile"] = resolved_profile
            return config

        current = modify_config(_set_profile)
    else:
        current = load_config()
    context = feishu_identity_context(verify=verify)
    source = _detect_agent_source()
    saved_source = str(current["feishu"].get("agent_source") or "")
    selected_identity = str(current["feishu"].get("identity") or "user")
    can_bind = source in {"openclaw", "hermes", "lark-channel"}
    context.update(
        {
            "agent_source_detected": source,
            "agent_source_configured": saved_source,
            "can_bind_current_agent": can_bind,
            "selected_identity": selected_identity,
            "identity_confirmed": True,
            "profile_resolution": profile_resolution,
            "manager_configured": bool(current["feishu"].get("manager_open_id")),
            "selection_rule": (
                "Use the current conversation App ID to select exactly one lark-cli "
                "profile. Never select by default status or bot display name."
            ),
            "binding_modes": {
                "agent": (
                    "Bind the detected Agent (OpenClaw/Hermes/Lark Channel) app after "
                    "explicit confirmation."
                    if can_bind
                    else "Unavailable: this Agent does not expose a supported app binding source."
                ),
                "existing": "Use and explicitly confirm the existing lark-cli App ID/profile.",
                "dedicated": (
                    "Initialize a dedicated Feishu app/profile; recommended for generic "
                    "Agents that cannot prove the conversational bot identity."
                ),
            },
        }
    )
    if not context["app_id_unambiguous"]:
        return context, "select_or_initialize_feishu_profile"
    selected = context[selected_identity]
    ready = bool(selected["available"]) and selected["status"] == "ready"
    if selected_identity == "user":
        ready = ready and selected.get("token_status") in {"", "valid"}
        if not ready:
            if _authorization(current)["state"] == "waiting":
                return context, "resume_existing_user_base_authorization"
            return context, "run_feishu_auth_start"
        return context, "reuse_existing_user_authorization_and_confirm_context"
    if not ready:
        return context, "configure_bot_credentials_and_scopes_without_user_auth"
    return context, "confirm_feishu_app_and_bot"


def _feishu_identity(identity: str) -> dict[str, Any]:
    state: dict[str, Any] = {}

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        previous = str(config["feishu"].get("identity") or "user")
        was_confirmed = bool(config["setup"]["feishu_identity_confirmed"])
        config["feishu"]["identity"] = identity
        config["setup"]["feishu_identity_confirmed"] = True
        state["previous"] = previous
        state["changed"] = previous != identity or not was_confirmed
        if state["changed"]:
            config["health"]["feishu"] = dict(DEFAULT_CONFIG["health"]["feishu"])
            _reset_authorization(config, identity)
            _reset_manager_access(config)
            config["feishu"]["manager_open_id"] = ""
            if config["feishu"].get("binding_mode") == "agent":
                config["feishu"]["binding_mode"] = ""
                config["feishu"]["agent_source"] = ""
                config["feishu"]["expected_app_id"] = ""
                config["feishu"]["cli_profile"] = ""
        return config

    config = modify_config(mutate)
    return {
        "identity": identity,
        "previous_identity": state["previous"],
        "identity_confirmed": True,
        "authorization_policy": (
            "reuse an existing valid user authorization; otherwise start one Base authorization flow"
            if identity == "user"
            else "use bot credentials and backend scopes; never start user authorization"
        ),
        "authorization": _public_authorization(_authorization(config)),
    }


def _feishu_app(app_id: str) -> dict[str, Any]:
    normalized = app_id.strip()
    if not re.fullmatch(r"cli_[A-Za-z0-9]+", normalized):
        raise ValueError("Feishu App ID must start with cli_ and contain only letters/digits")
    profile = profile_name_for_app(normalized)
    state: dict[str, Any] = {"target_cleared": False}

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        if not config["setup"]["feishu_identity_confirmed"]:
            raise ValueError("select user or bot identity before selecting the Feishu app")
        previous = str(config["feishu"].get("expected_app_id") or "")
        config["feishu"]["expected_app_id"] = normalized
        config["feishu"]["cli_profile"] = profile
        if not config["feishu"].get("binding_mode"):
            config["feishu"]["binding_mode"] = "existing"
        # The first selection has no previous app, so a table bound before the
        # app was chosen stays in place. Switching from one real app to another
        # drops the old target because the grant does not carry over.
        if previous and previous != normalized:
            state["target_cleared"] = True
            config["health"]["feishu"] = dict(DEFAULT_CONFIG["health"]["feishu"])
            _reset_authorization(config, config["feishu"]["identity"])
            config["feishu"].update(
                {
                    "enabled": False,
                    "expected_user_open_id": "",
                    "manager_open_id": "",
                    "base_token": "",
                    "table_id": "",
                    "base_url": "",
                    "provisioning": "",
                    "field_mapping": {},
                }
            )
            _reset_manager_access(config)
        return config

    modify_config(mutate)
    return {
        "app_selected": True,
        "app_id_included": False,
        "target_cleared": state["target_cleared"],
        "private_profile": profile,
        "global_profiles_modified": False,
        "next_command": (
            "lark config init --app-id <CONFIRMED_APP_ID> "
            "--app-secret-stdin"
        ),
        "profile_name_added_automatically": True,
    }


def _feishu_local_profile(
    arguments: argparse.Namespace,
) -> tuple[dict[str, Any], str]:
    """Inspect or import one existing user-level lark-cli app safely."""
    inventory = discover_global_lark_profiles()
    if arguments.local_profile_command == "scan":
        try:
            config = load_config()
        except ConfigError:
            expected_app_id = ""
            private_profile = ""
        else:
            expected_app_id = _expected_app_id(config)
            private_profile = str(config["feishu"].get("cli_profile") or "").strip()
        matching = [
            item
            for item in inventory["profiles"]
            if item["app_id"] == expected_app_id
        ]
        return {
            **inventory,
            "selected_app_id": expected_app_id,
            "private_profile": private_profile,
            "selected_match_count": len(matching),
            "read_only": True,
            "original_config_modified": False,
        }, (
            "select_feishu_app"
            if not expected_app_id
            else (
                "reuse_or_configure_private_lark_profile"
                if len(matching) == 1
                else "configure_private_lark_profile"
            )
        )

    config = load_config()
    if not config["setup"]["feishu_identity_confirmed"]:
        raise ConfigError("confirm Feishu identity before importing a local profile")
    expected_app_id = _expected_app_id(config)
    private_profile = str(config["feishu"].get("cli_profile") or "").strip()
    if not expected_app_id or not private_profile:
        raise ConfigError(
            "select the exact App ID with manage feishu-app before importing a local profile"
        )
    matching = [
        item for item in inventory["profiles"] if item["app_id"] == expected_app_id
    ]
    if len(matching) != 1:
        raise ConfigError(
            f"expected exactly one existing local profile for App ID {expected_app_id}; "
            f"found {len(matching)}"
        )
    selected = matching[0]
    if not selected["app_secret_available"]:
        raise ConfigError(
            "the selected local profile has no reusable App credential; configure the "
            "isolated profile through secret stdin"
        )
    if not arguments.yes:
        return {
            "preview": {
                "source_config": inventory["path"],
                "source_profile": selected["name"],
                "app_id": expected_app_id,
                "target_private_profile": private_profile,
                "app_secret_storage": selected["app_secret_storage"],
                "copies_app_credential": True,
                "copies_user_tokens": False,
                "modifies_original_config": False,
                "secret_values_displayed": False,
            }
        }, "rerun_with_yes"
    result = import_global_lark_profile(expected_app_id, private_profile)
    return result, "run_feishu_context_then_authorize_only_if_needed"


def _feishu_create_base(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    config = load_config()
    if config["feishu"]["destination"] != "create":
        raise LarkCLIError(
            "Feishu Base creation requires the explicit destination=create choice",
            kind="confirmation",
        )
    has_token = bool(str(config["feishu"].get("base_token") or "").strip())
    has_table = bool(str(config["feishu"].get("table_id") or "").strip())
    resuming = created_target_is_resumable(config["feishu"])
    schema = standard_field_schema()
    base_name = " ".join(str(arguments.name).split())
    table_name = " ".join(str(arguments.table_name).split())
    manager_access = str(config["feishu"].get("manager_access") or "undecided")
    preview = {
        "base_name": base_name,
        "table_name": table_name,
        "identity": config["feishu"]["identity"],
        "field_count": len(schema),
        "field_names": [field["name"] for field in schema],
        "transport": "native lark-cli binary with an argv array; no shell JSON",
        "global_profiles_modified": False,
        "resuming_existing_base": resuming,
    }
    preview["authorization_source"] = "current_command"
    if not arguments.yes:
        return {
            "preview": preview,
            "created": False,
        }, "rerun_with_yes"
    if (has_token or has_table) and not resuming:
        raise LarkCLIError(
            "a Feishu target is already configured; refusing to create another Base "
            "without a new target decision",
            kind="config",
        )
    if resuming:
        stored_base_name = str(
            config["feishu"].get("created_base_name") or ""
        ).strip()
        stored_table_name = str(
            config["feishu"].get("created_table_name") or ""
        ).strip()
        if stored_base_name and base_name != stored_base_name:
            raise LarkCLIError(
                f"the earlier Base was created as {stored_base_name!r}; rerun with "
                "the same --name to resume it",
                kind="config",
            )
        if stored_table_name and table_name != stored_table_name:
            raise LarkCLIError(
                f"the earlier Base table was created as {stored_table_name!r}; "
                "rerun with the same --table-name to resume it",
                kind="config",
            )
    if not config["setup"]["feishu_identity_confirmed"]:
        raise LarkCLIError("confirm Feishu identity before Base creation", kind="config")
    identity = config["feishu"]["identity"]
    if (
        not config["feishu"].get("cli_profile")
        and config["feishu"].get("binding_mode") != "agent"
    ):
        raise LarkCLIError(
            "select the Skill-owned Feishu app/profile before Base creation",
            kind="config",
        )
    if identity == "bot":
        raise LarkCLIError(
            "bot Base creation is disabled because this runtime cannot authenticate "
            "a host-event sender; use user identity or an existing Base",
            kind="confirmation",
        )
    if manager_access != "approved":
        raise LarkCLIError(
            "Base creation requires the user's approved management-access choice",
            kind="confirmation",
        )
    if (
        config["feishu"].get("manager_access_base_name") != base_name
        or config["feishu"].get("manager_access_table_name") != table_name
    ):
        raise LarkCLIError(
            "management-access approval does not match this Base/table",
            kind="confirmation",
        )
    verify_feishu_identity(config["feishu"], identity=identity)
    recreated_after_deletion = False
    if resuming:
        try:
            list_fields(
                str(config["feishu"]["base_token"]),
                str(config["feishu"]["table_id"]),
                identity=identity,
            )
        except LarkCLIError as exc:
            if exc.kind != "target_deleted":
                raise
            # The recorded Base is gone on the Feishu side, so there is nothing
            # to resume and a fresh create cannot duplicate it. The exact-name
            # and manager-approval checks above still gate this path.
            resuming = False
            recreated_after_deletion = True
    if resuming:
        base_token = str(config["feishu"]["base_token"])
        table_id = str(config["feishu"]["table_id"])
        document_url = feishu_document_url(config["feishu"])
    else:
        payload = create_standard_base(
            base_name,
            table_name,
            identity=identity,
        )
        base_token, table_id = created_base_identifiers(payload)
        document_url = feishu_document_url(
            {
                "base_url": created_base_document_url(payload),
                "table_id": table_id,
            }
        )

    # Persist the recovery anchor before any external permission/schema step,
    # so a later failure can resume from this exact state.
    def mutate_created(config: dict[str, Any]) -> dict[str, Any]:
        config["feishu"].update(
            {
                "enabled": False,
                "base_token": base_token,
                "table_id": table_id,
                "base_url": document_url
                or str(config["feishu"].get("base_url") or "").strip(),
                "provisioning": "created",
                "field_mapping": {},
                "created_base_name": str(
                    config["feishu"].get("created_base_name") or ""
                ).strip()
                or base_name,
                "created_table_name": str(
                    config["feishu"].get("created_table_name") or ""
                ).strip()
                or table_name,
            }
        )
        return config

    config = modify_config(mutate_created)
    check = preflight_feishu(config["feishu"])

    def mutate_complete(config: dict[str, Any]) -> dict[str, Any]:
        config["feishu"].update(
            {
                "enabled": True,
                "field_mapping": check["mapping"],
            }
        )
        return config

    config = modify_config(mutate_complete)
    update_health("feishu", success=True)
    return {
        "created": True,
        **preview,
        "target_configured": True,
        "resource_tokens_included": False,
        "creation_identity": "user",
        "separate_manager_grant_performed": False,
        "field_mapping_saved": True,
        "resumed_existing": resuming,
        "recreated_after_deletion": recreated_after_deletion,
        "authorization_source": "current_command",
        "document_url": feishu_document_url(config["feishu"]) or document_url,
    }, "none"


def _feishu_manager_access(
    mode: str, *, base_name: str | None = None, table_name: str | None = None
) -> dict[str, Any]:
    selected = "approved" if mode == "approve" else "declined"
    normalized_base_name = " ".join(str(base_name or "").split())
    normalized_table_name = " ".join(str(table_name or "").split())
    if selected == "approved" and not (normalized_base_name and normalized_table_name):
        raise ValueError("approve requires --base-name and --table-name")

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        if config["feishu"]["destination"] != "create":
            raise ValueError("management access is only selectable for a new Base")
        if not config["setup"]["feishu_identity_confirmed"]:
            raise ValueError("confirm Feishu identity before choosing management access")
        if config["feishu"]["identity"] != "user":
            raise ValueError(
                "management access for a new Base requires user identity; "
                "bot-created Base grants are disabled"
            )
        _set_manager_access(
            config,
            selected,
            base_name=normalized_base_name if selected == "approved" else "",
            table_name=normalized_table_name if selected == "approved" else "",
        )
        return config

    modify_config(mutate)
    return {
        "manager_access": selected,
        "creation_identity": "user",
        "external_resource_changed": False,
        "approval_scoped_to_names": selected == "approved",
    }


def _identity_ready(context: dict[str, Any], identity: str) -> bool:
    selected = context.get(identity)
    if not isinstance(selected, dict):
        return False
    ready = bool(selected.get("available")) and selected.get("status") == "ready"
    if identity == "user":
        ready = ready and selected.get("token_status") in {"", "valid"}
    return ready


def _save_authorization_state(
    state: str,
    *,
    started: bool = False,
    completed: bool = False,
    extras: dict[str, str] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        authorization = _authorization(config)
        authorization["state"] = state
        authorization["identity"] = config["feishu"]["identity"]
        authorization["updated_at"] = now
        if started:
            authorization["started_at"] = now
        if completed:
            authorization["completed_at"] = now
        if state in {"waiting", "expired", "failed", "not_started"}:
            authorization["completed_at"] = ""
        if extras:
            for key, value in extras.items():
                authorization[key] = value
        if state != "waiting":
            authorization["device_code"] = ""
            authorization["verification_url"] = ""
            authorization["hint"] = ""
            authorization["expires_in"] = ""
        return config

    config = modify_config(mutate)
    return _public_authorization(_authorization(config))


def _feishu_auth(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    config = load_config()
    if not config["setup"]["feishu_identity_confirmed"]:
        return {
            "identity_confirmed": False,
            "authorization": _public_authorization(_authorization(config)),
        }, "ask_feishu_identity_before_authorization"
    identity = config["feishu"]["identity"]
    authorization = _authorization(config)
    if arguments.auth_command == "status":
        authorization_verified = False
        if identity == "user" and authorization.get("state") == "authorized":
            try:
                authorization_verified = _identity_ready(
                    feishu_identity_context(verify=True), "user"
                )
            except LarkCLIError:
                authorization_verified = False
        elif identity == "bot":
            authorization_verified = True
        if authorization["state"] == "waiting":
            next_action = "open_verification_url_then_complete_feishu_auth"
        elif identity == "user" and not authorization_verified:
            next_action = "run_feishu_auth_start"
        else:
            next_action = "none"
        return {
            "identity": identity,
            "authorization": _public_authorization(authorization),
            "authorization_verified": authorization_verified,
            "secrets_included": False,
            "verification_url": authorization.get("verification_url") or "",
        }, next_action
    if arguments.auth_command == "expire":
        if not arguments.yes:
            return {
                "preview": "mark the current user authorization flow expired",
                "authorization": _public_authorization(authorization),
            }, "rerun_with_yes"
        return {
            "identity": identity,
            "authorization": _save_authorization_state("expired"),
        }, "run_feishu_auth_start"
    if identity == "bot":
        state = _save_authorization_state("not_required", completed=True)
        return {
            "identity": "bot",
            "authorization": state,
            "user_authorization_started": False,
        }, "configure_bot_credentials_and_scopes_without_user_auth"
    if arguments.auth_command == "start":
        if waiting_login_is_resumable(authorization):
            verification_url = str(authorization.get("verification_url") or "")
            return {
                "identity": identity,
                "authorization": _public_authorization(authorization),
                "new_authorization_started": False,
                "verification_url": verification_url,
                "qr_code_path": write_verification_qr(verification_url),
            }, "open_verification_url_then_complete_feishu_auth"
        context = feishu_identity_context(verify=True)
        if context.get("app_id_unambiguous") is False:
            return {
                "identity": identity,
                "authorization": _public_authorization(authorization),
                "new_authorization_started": False,
            }, "select_or_initialize_feishu_profile"
        if _identity_ready(context, "user"):
            state = _save_authorization_state("authorized", completed=True)
            return {
                "identity": identity,
                "authorization": state,
                "new_authorization_started": False,
                "existing_authorization_reused": True,
            }, "confirm_feishu_app_and_user"
        fields = start_user_device_login()
        state = _save_authorization_state(
            "waiting",
            started=True,
            extras={
                "device_code": fields["device_code"],
                "verification_url": fields["verification_url"],
                "hint": fields.get("hint") or "",
                "expires_in": fields.get("expires_in") or "",
            },
        )
        return {
            "identity": identity,
            "authorization": state,
            "new_authorization_started": True,
            "verification_url": fields["verification_url"],
            "qr_code_path": write_verification_qr(fields["verification_url"]),
            "device_code_persisted": True,
            "secrets_included": False,
        }, "open_verification_url_then_complete_feishu_auth"
    context = feishu_identity_context(verify=True)
    if context.get("app_id_unambiguous") is False:
        return {
            "identity": identity,
            "authorization": _public_authorization(authorization),
            "authorization_verified": False,
        }, "select_or_initialize_feishu_profile"
    if _identity_ready(context, "user"):
        state = _save_authorization_state("authorized", completed=True)
        return {
            "identity": identity,
            "authorization": state,
            "authorization_verified": True,
        }, "confirm_feishu_app_and_user"
    if authorization["state"] != "waiting":
        return {
            "identity": identity,
            "authorization": _public_authorization(authorization),
            "authorization_verified": False,
            "new_authorization_started": False,
        }, "run_feishu_auth_start"
    device_code = str(authorization.get("device_code") or "").strip()
    if not device_code:
        return {
            "identity": identity,
            "authorization": _public_authorization(authorization),
            "authorization_verified": False,
        }, "run_feishu_auth_start"
    try:
        complete_user_device_login(device_code)
    except LarkCLIError as exc:
        action = complete_authorization_action(identity_ready=False, error=exc)
        if action == "keep_waiting":
            return {
                "identity": identity,
                "authorization": _public_authorization(authorization),
                "authorization_verified": False,
                "verification_url": authorization.get("verification_url") or "",
            }, "open_verification_url_then_complete_feishu_auth"
        if action == "expired":
            return {
                "identity": identity,
                "authorization": _save_authorization_state("expired"),
                "authorization_verified": False,
            }, "run_feishu_auth_start"
        raise
    context = feishu_identity_context(verify=True)
    action = complete_authorization_action(
        identity_ready=_identity_ready(context, "user"),
        error=None,
    )
    if action != "authorized":
        return {
            "identity": identity,
            "authorization": _public_authorization(authorization),
            "authorization_verified": False,
            "verification_url": authorization.get("verification_url") or "",
        }, "open_verification_url_then_complete_feishu_auth"
    state = _save_authorization_state("authorized", completed=True)
    return {
        "identity": identity,
        "authorization": state,
        "authorization_verified": True,
        "new_authorization_started": False,
    }, "confirm_feishu_app_and_user"


def _feishu_disable(*, yes: bool) -> tuple[dict[str, Any], str]:
    if not yes:
        return {"preview": "disable Feishu sync; no Base data is deleted"}, "rerun_with_yes"

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        config["feishu"]["enabled"] = False
        return config

    modify_config(mutate)
    return {"disabled": True, "base_data_deleted": False}, "none"


def _app_init_state_path() -> Path:
    return data_dir() / "feishu-app-init.json"


def _read_app_init_state() -> dict[str, Any]:
    path = _app_init_state_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pid_alive(pid: object) -> bool:
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except OSError:
        return False
    return True


def _public_app_init(state: dict[str, Any], **extra: Any) -> dict[str, Any]:
    data = {
        "verification_url": str(state.get("verification_url") or ""),
        "qr_code_path": str(state.get("qr_code_path") or ""),
        "running": _pid_alive(state.get("pid")),
        "app_id_included": False,
        "secrets_included": False,
    }
    data.update(extra)
    return data


def _rename_isolated_profile(old: str, new: str) -> None:
    if not old or old == new:
        return
    work = lark_cli_work_dir()
    work.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [resolve_lark_cli(), "profile", "rename", old, new],
        cwd=work,
        env=isolated_cli_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("could not move the new Feishu app into the skill-owned profile")


def _bind_created_app(app_id: str) -> tuple[dict[str, Any], str]:
    """Rename the new profile and record the App ID without echoing it."""
    current = next(
        (item for item in isolated_lark_apps() if item["app_id"] == app_id),
        None,
    )
    target_name = profile_name_for_app(app_id)
    old_name = current["name"] if current and current["name"] else app_id
    _rename_isolated_profile(old_name, target_name)
    state = _read_app_init_state()
    try:
        config = load_config()
    except ConfigError:
        config = None
    if config is None or not config["setup"]["feishu_identity_confirmed"]:
        state["pending_app_id"] = app_id
        state["pid"] = 0
        secure_write_json(_app_init_state_path(), state)
        return _public_app_init(
            state, app_created=True
        ), "select_feishu_identity_then_bind_app"
    selected = _feishu_app(app_id)
    state["pending_app_id"] = ""
    state["pid"] = 0
    secure_write_json(_app_init_state_path(), state)
    action = (
        "rebind_feishu_table"
        if selected.get("target_cleared")
        else "authorize_feishu_user"
    )
    return _public_app_init(
        state,
        app_created=True,
        app_selected=True,
        target_cleared=bool(selected.get("target_cleared")),
        private_profile=selected.get("private_profile") or target_name,
    ), action


def _app_init_log_text() -> str:
    state = _read_app_init_state()
    raw_path = str(state.get("log_path") or "")
    if not raw_path:
        return ""
    path = Path(raw_path)
    try:
        path.resolve().relative_to(lark_cli_work_dir())
    except ValueError:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:65536]
    except OSError:
        return ""


def _feishu_app_init_status() -> tuple[dict[str, Any], str]:
    state = _read_app_init_state()
    if not state:
        return {
            "started": False,
            "app_id_included": False,
            "secrets_included": False,
        }, "run_feishu_app_init"
    pending = str(state.get("pending_app_id") or "").strip()
    if pending:
        try:
            config = load_config()
        except ConfigError:
            config = None
        if config is not None and config["setup"]["feishu_identity_confirmed"]:
            return _bind_created_app(pending)
        return _public_app_init(state, app_created=True), "select_feishu_identity_then_bind_app"
    parsed = parse_feishu_app_init_output(_app_init_log_text())
    app_id = str(parsed.get("app_id") or "")
    if parsed.get("succeeded") and app_id:
        return _bind_created_app(app_id)
    url = str(parsed.get("verification_url") or state.get("verification_url") or "")
    if url and url != state.get("verification_url"):
        state["verification_url"] = url
        state["qr_code_path"] = write_verification_qr(url)
        secure_write_json(_app_init_state_path(), state)
    if _pid_alive(state.get("pid")):
        return _public_app_init(
            state
        ), "open_verification_url_then_check_feishu_app_init"
    return _public_app_init(
        state,
        started=True,
        failed=True,
    ), "run_feishu_app_init"


def _start_feishu_app_init() -> tuple[dict[str, Any], str]:
    state = _read_app_init_state()
    if _pid_alive(state.get("pid")):
        return _public_app_init(
            state, new_init_started=False
        ), "open_verification_url_then_check_feishu_app_init"
    if isolated_lark_apps():
        return {
            "existing_app_present": True,
            "app_id_included": False,
            "secrets_included": False,
        }, "select_existing_feishu_app"
    work = lark_cli_work_dir()
    work.mkdir(parents=True, exist_ok=True)
    log_path = work / "feishu-app-init.log"
    log_handle = log_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [
                resolve_lark_cli(),
                "config",
                "init",
                "--new",
                "--brand",
                "feishu",
                "--lang",
                "zh",
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=work,
            env=isolated_cli_environment(),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        log_handle.close()
        raise FileNotFoundError("lark-cli is not installed") from exc
    log_handle.close()
    state = {
        "pid": process.pid,
        "log_path": str(log_path),
        "verification_url": "",
        "qr_code_path": "",
        "pending_app_id": "",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    secure_write_json(_app_init_state_path(), state)
    deadline = time.time() + 20
    parsed: dict[str, Any] = {}
    while time.time() < deadline:
        parsed = parse_feishu_app_init_output(_app_init_log_text())
        if parsed.get("verification_url") or parsed.get("app_id"):
            break
        if process.poll() is not None:
            break
        time.sleep(0.4)
    url = str(parsed.get("verification_url") or "")
    state["verification_url"] = url
    state["qr_code_path"] = write_verification_qr(url) if url else ""
    secure_write_json(_app_init_state_path(), state)
    if parsed.get("succeeded") and parsed.get("app_id"):
        return _bind_created_app(str(parsed["app_id"]))
    if url:
        return _public_app_init(
            state, new_init_started=True
        ), "open_verification_url_then_check_feishu_app_init"
    if process.poll() is not None:
        return _public_app_init(
            state, new_init_started=True, failed=True
        ), "run_feishu_app_init"
    return _public_app_init(
        state, new_init_started=True
    ), "check_feishu_app_init_status"


def _feishu_app_init(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if arguments.init_command == "status":
        return _feishu_app_init_status()
    if not arguments.yes:
        if isolated_lark_apps():
            return {
                "existing_app_present": True,
                "isolated_config_only": True,
                "app_id_included": False,
                "secrets_included": False,
            }, "select_existing_feishu_app"
        return {
            "preview": "create one Feishu app inside the skill-owned lark-cli config",
            "isolated_config_only": True,
            "existing_app_present": False,
            "app_id_included": False,
            "secrets_included": False,
        }, "rerun_with_yes"
    return _start_feishu_app_init()


def _settings(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if arguments.settings_command == "show":
        config = load_config() if config_path().exists() else validate_config(deepcopy(DEFAULT_CONFIG))
        return {"settings": config["settings"]}, "none"
    updates: dict[str, Any] = {}
    if arguments.min_score is not None:
        updates["min_score"] = arguments.min_score
    if arguments.content_dedup is not None:
        updates["content_dedup"] = arguments.content_dedup == "on"
    if arguments.rubric is not None:
        updates["rubric"] = arguments.rubric
    if not updates:
        raise ValueError("provide --min-score, --content-dedup, and/or --rubric")

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        config["settings"].update(updates)
        return config

    saved = modify_config(mutate)
    return {"settings": saved["settings"], "updated_fields": sorted(updates)}, "none"


def _preferences(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    config = load_config()
    current = config["preferences"]
    if arguments.preference_command == "show":
        return {"preferences": current}, "none"
    if arguments.preference_command == "clear":
        if not arguments.yes:
            return {
                "preview": dict(DEFAULT_CONFIG["preferences"]),
                "current": current,
            }, "rerun_with_yes"

        def mutate_clear(config: dict[str, Any]) -> dict[str, Any]:
            config["preferences"] = dict(DEFAULT_CONFIG["preferences"])
            return config

        saved = modify_config(mutate_clear)
        return {"preferences": saved["preferences"], "cleared": True}, "none"
    updates: dict[str, Any] = {}
    list_updates = {
        "include_topics": arguments.include_topic,
        "exclude_keywords": arguments.exclude_keyword,
        "preferred_accounts": arguments.preferred_account,
    }
    for key, values in list_updates.items():
        if values is not None:
            cleaned = list(
                dict.fromkeys(" ".join(value.split()) for value in values if value.strip())
            )
            updates[key] = cleaned
    if arguments.digest_hours is not None:
        updates["digest_hours"] = arguments.digest_hours
    if arguments.digest_limit is not None:
        updates["digest_limit"] = arguments.digest_limit
    if not updates:
        raise ValueError("provide at least one preference update")

    def mutate_update(config: dict[str, Any]) -> dict[str, Any]:
        config["preferences"].update(updates)
        return config

    saved = modify_config(mutate_update)
    return {"preferences": saved["preferences"], "updated_fields": sorted(updates)}, "none"


# The all-data wipe enumerates every child of the state directory. Refuse
# well-known shared/system directory names regardless of override, and refuse
# any directory that contains no known application artifact.
RESET_SHARED_ROOT_NAMES = {
    "tmp",
    "temp",
    "var",
    "etc",
    "usr",
    "opt",
    "srv",
    "bin",
    "sbin",
    "library",
    "system",
    "windows",
    "users",
    "home",
    "shared",
    "public",
    "desktop",
    "documents",
    "downloads",
}
RESET_STATE_ARTIFACTS = (
    "config.json",
    "queue.json",
    "config.lock",
    "queue.lock",
    "venv",
    "lark-cli",
    "lark-cli-config",
    "lark-cli-home",
    "lark-cli-work",
)


def _reset_root_is_dangerous(resolved_root: Path) -> bool:
    if resolved_root == resolved_root.parent:
        return True
    if resolved_root == Path.home().resolve():
        return True
    if resolved_root.name.casefold() in RESET_SHARED_ROOT_NAMES:
        return True
    if not resolved_root.is_dir():
        return False
    try:
        children = list(resolved_root.iterdir())
    except OSError:
        return True
    if not children:
        return False
    if resolved_root.name == APP_NAME:
        return False
    return not any(
        (resolved_root / artifact).exists() for artifact in RESET_STATE_ARTIFACTS
    )


def _reset(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    scope = arguments.scope
    targets: list[Path] = []
    if scope in {"queue", "all-data"}:
        targets.extend([queue_path(), lock_path()])
        cache_root = article_cache_dir()
        if cache_root.exists():
            targets.append(cache_root)
    if scope == "all-data":
        root = config_path().parent
        if _reset_root_is_dangerous(root.resolve()):
            raise ValueError(
                "refusing all-data reset: the state directory is not a "
                "dedicated application directory"
            )
        targets.append(config_path())
        for pattern in (
            "config.v*.backup.json",
            ".agent-config-*.json",
            "feishu-auth-qr*.png",
        ):
            targets.extend(root.glob(pattern))
        targets.extend(
            [
                root / "lark-cli-config",
                root / "lark-cli-home",
                root / "lark-cli-work",
            ]
        )
        # Full reset is allowlist-based so legacy or future state files cannot
        # silently survive and contaminate a clean-start test. Keep only the
        # installed runtimes, which are code/dependencies rather than user data.
        if root.is_dir():
            targets.extend(
                child
                for child in root.iterdir()
                if child.name not in {"venv", "lark-cli"}
            )
    existing = sorted({path.resolve() for path in targets if path.exists()}, key=str)
    if not arguments.yes:
        return {"preview": [str(path) for path in existing], "deleted": []}, "rerun_with_yes"
    if scope == "feishu":
        def mutate_reset(config: dict[str, Any]) -> dict[str, Any]:
            config["setup"]["feishu_identity_confirmed"] = False
            config["setup"]["feishu_authorization"] = dict(
                DEFAULT_CONFIG["setup"]["feishu_authorization"]
            )
            config["feishu"].update({
                "destination": "undecided",
                "enabled": False,
                "binding_mode": "",
                "agent_source": "",
                "expected_app_id": "",
                "cli_profile": "",
                "expected_user_open_id": "",
                "manager_open_id": "",
                "base_token": "",
                "table_id": "",
                "base_url": "",
                "field_mapping": {},
                "provisioning": "",
                "created_base_name": "",
                "created_table_name": "",
            })
            _reset_manager_access(config)
            config["health"] = validate_config(DEFAULT_CONFIG)["health"]
            return config

        modify_config(mutate_reset)
        return {"cleared": "feishu", "preserved": ["settings", "preferences", "queue"]}, "provide_article_link"
    root = data_dir().resolve()
    for target in existing:
        if target.parent != root and target not in {
            (root / "lark-cli-config").resolve(),
            (root / "lark-cli-home").resolve(),
            (root / "lark-cli-work").resolve(),
        }:
            raise ValueError(f"refusing to delete state outside the application directory: {target}")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    if scope in {"queue", "all-data"}:
        clear_article_cache()
    return {"deleted": [str(path) for path in existing], "recoverable": False}, "none"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--online", action="store_true")
    commands.add_parser("status")
    commands.add_parser("config-show")
    destination = commands.add_parser("feishu-destination")
    destination.add_argument(
        "--mode",
        choices=("skip", "existing", "create"),
        required=True,
    )
    target = commands.add_parser("feishu-target")
    target.add_argument(
        "--url-stdin",
        action="store_true",
        required=True,
        help="read one exact existing Base table URL from stdin without echoing tokens",
    )
    target.add_argument("--yes", action="store_true")
    host_context = commands.add_parser("feishu-host-context")
    host_sources = host_context.add_mutually_exclusive_group(required=True)
    host_sources.add_argument(
        "--agent-stdin", action="store_true", help="read host context JSON from stdin"
    )
    host_sources.add_argument(
        "--agent-file",
        type=Path,
        help="read trusted host context JSON from a UTF-8 file (Windows-safe)",
    )
    context = commands.add_parser("feishu-context")
    context.add_argument("--verify", action="store_true")
    identity = commands.add_parser("feishu-identity")
    identity.add_argument("--as", dest="identity", choices=("user", "bot"), required=True)
    app = commands.add_parser("feishu-app")
    app.add_argument("--app-id", required=True)
    app_init = commands.add_parser("feishu-app-init")
    app_init.add_argument("init_command", nargs="?", choices=("status",))
    app_init.add_argument("--yes", action="store_true")
    local_profile = commands.add_parser("feishu-local-profile")
    local_profile_commands = local_profile.add_subparsers(
        dest="local_profile_command", required=True
    )
    local_profile_commands.add_parser("scan")
    import_profile = local_profile_commands.add_parser("import")
    import_profile.add_argument("--yes", action="store_true")
    manager_access = commands.add_parser("feishu-manager-access")
    manager_access.add_argument("--mode", choices=("approve", "decline"), required=True)
    manager_access.add_argument("--base-name")
    manager_access.add_argument("--table-name")
    create_base = commands.add_parser("feishu-create-base")
    create_base.add_argument("--name", required=True)
    create_base.add_argument("--table-name", required=True)
    create_base.add_argument("--yes", action="store_true")
    auth = commands.add_parser("feishu-auth")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_commands.add_parser("status")
    auth_commands.add_parser("start")
    auth_commands.add_parser("complete")
    expire = auth_commands.add_parser("expire")
    expire.add_argument("--yes", action="store_true")
    settings = commands.add_parser("settings")
    settings_commands = settings.add_subparsers(dest="settings_command", required=True)
    settings_commands.add_parser("show")
    set_settings = settings_commands.add_parser("set")
    set_settings.add_argument("--min-score", type=float)
    set_settings.add_argument("--content-dedup", choices=("on", "off"))
    set_settings.add_argument("--rubric", choices=("technical", "content_ops"))
    preferences = commands.add_parser("preferences")
    preference_commands = preferences.add_subparsers(
        dest="preference_command", required=True
    )
    preference_commands.add_parser("show")
    set_preferences = preference_commands.add_parser("set")
    set_preferences.add_argument("--include-topic", action="append")
    set_preferences.add_argument("--exclude-keyword", action="append")
    set_preferences.add_argument("--preferred-account", action="append")
    set_preferences.add_argument("--digest-hours", type=int)
    set_preferences.add_argument("--digest-limit", type=int)
    clear_preferences = preference_commands.add_parser("clear")
    clear_preferences.add_argument("--yes", action="store_true")
    disable = commands.add_parser("feishu-disable")
    disable.add_argument("--yes", action="store_true")
    reset = commands.add_parser("reset")
    reset.add_argument("--scope", choices=("feishu", "queue", "all-data"), required=True)
    reset.add_argument("--yes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        next_action = "none"
        if arguments.command == "doctor":
            data, next_action = _doctor(online=arguments.online)
        elif arguments.command == "status":
            data, next_action = _status()
        elif arguments.command == "config-show":
            data = redacted_config(load_config())
        elif arguments.command == "feishu-destination":
            data, next_action = _feishu_destination(arguments.mode)
        elif arguments.command == "feishu-target":
            data, next_action = _feishu_target(arguments)
        elif arguments.command == "feishu-host-context":
            data, next_action = _import_feishu_host_context(arguments)
        elif arguments.command == "feishu-context":
            data, next_action = _feishu_context(verify=arguments.verify)
        elif arguments.command == "feishu-identity":
            data = _feishu_identity(arguments.identity)
            next_action = "run_feishu_context_then_authorize_only_if_needed"
        elif arguments.command == "feishu-app":
            data = _feishu_app(arguments.app_id)
            next_action = (
                "rebind_feishu_table"
                if data.get("target_cleared")
                else "reuse_or_configure_private_lark_profile"
            )
        elif arguments.command == "feishu-app-init":
            data, next_action = _feishu_app_init(arguments)
        elif arguments.command == "feishu-local-profile":
            data, next_action = _feishu_local_profile(arguments)
        elif arguments.command == "feishu-manager-access":
            data = _feishu_manager_access(
                arguments.mode,
                base_name=arguments.base_name,
                table_name=arguments.table_name,
            )
            next_action = (
                "preview_feishu_base_creation"
                if arguments.mode == "approve"
                else "choose_existing_base_or_user_identity"
            )
        elif arguments.command == "feishu-create-base":
            data, next_action = _feishu_create_base(arguments)
        elif arguments.command == "feishu-auth":
            data, next_action = _feishu_auth(arguments)
        elif arguments.command == "settings":
            data, next_action = _settings(arguments)
        elif arguments.command == "preferences":
            data, next_action = _preferences(arguments)
        elif arguments.command == "feishu-disable":
            data, next_action = _feishu_disable(yes=arguments.yes)
        else:
            data, next_action = _reset(arguments)
        envelope = success(data, next_action=next_action)
        print(dump(envelope) if arguments.format == "json" else json.dumps(envelope, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        envelope = failure(exc)
        print(dump(envelope) if arguments.format == "json" else json.dumps(envelope, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
