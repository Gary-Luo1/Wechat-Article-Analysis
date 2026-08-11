#!/usr/bin/env python3
"""Read, score, complete, export, and synchronize queued articles."""

from __future__ import annotations

import argparse
import contextlib
from copy import deepcopy
import io
import json
import logging
from pathlib import Path
from typing import Any

from article_inbox import plan_digest, query_inbox
from bitable_client import (
    LarkCLIError,
    standard_field_schema,
)
from config_store import DEFAULT_CONFIG, ConfigError, load_config, modify_config, update_health
from feishu_target import production_feishu_target
from http_client import RequestPacer, new_session
from protocol import dump, failure, success
from queue_helpers import (
    cleanup_processed,
    complete_article,
    dismiss_article,
    export_queue,
    get_pending,
    get_processed_entry,
    add_pending_with_verified_read,
    pending_sync_entries,
    read_queue,
    has_verified_read,
    record_verified_read,
    resolve_pending,
    restore_dismissed,
    update_inbox_item,
    update_sync_status,
)
from scoring_rubric import (
    calculate_score,
    format_rationale,
    is_advertisement,
    should_sync,
)
from url_identity import canonicalize_wechat_article_url


logger = logging.getLogger("wechat-process")
MAX_BATCH_CONTENT_CHARS = 200_000


def fetch_article(url: str, **kwargs: Any) -> dict[str, Any]:
    from article_reader import fetch_article as fetch

    return fetch(url, **kwargs)


def fetch_article_text(url: str, **kwargs: Any) -> str:
    from article_reader import fetch_article_text as fetch

    return fetch(url, **kwargs)


class ArticleReadRequiredError(ValueError):
    """A scoreable article must have been read in a prior command invocation."""

    code = "ARTICLE_READ_REQUIRED"
    retryable = False
    next_action = "read_article_before_completion"

    def __init__(self) -> None:
        super().__init__("read the article successfully before scoring or completing it")


class BatchRiskControlError(ValueError):
    """Expose a safe, structured stop point for automated batch readers."""

    code = "ARTICLE_RISK_CONTROL"
    retryable = False
    next_action = "wait_before_retry"

    def __init__(self, article: dict[str, Any], successful: int) -> None:
        super().__init__(f"WeChat risk control stopped the batch after {successful} successful article(s)")
        self.details = {
            "blocked_url": str(article.get("link", "")),
            "successful": successful,
        }


class BatchReadError(ValueError):
    """Summarize batch item failures without losing their retry semantics."""

    def __init__(self, failures: list[Exception], successful: int) -> None:
        primary = next(
            (
                failure
                for failure in failures
                if not bool(getattr(failure, "retryable", False))
            ),
            failures[0],
        )
        self.code = str(getattr(primary, "code", "ARTICLE_FETCH_FAILED"))
        self.retryable = all(bool(getattr(failure, "retryable", False)) for failure in failures)
        self.next_action = str(getattr(primary, "next_action", "inspect_failed_items"))
        failure_codes = [
            str(getattr(failure, "code", "ARTICLE_FETCH_FAILED"))
            for failure in failures
        ]
        self.details = {
            "successful": successful,
            "failed": len(failures),
            "failure_codes": failure_codes,
        }
        super().__init__(
            f"batch read completed with {len(failures)} failed article(s); "
            f"{successful} succeeded"
        )


def _resolve(arguments: argparse.Namespace) -> dict[str, Any]:
    index = arguments.index - 1 if arguments.index is not None else None
    return resolve_pending(index=index, link=arguments.link)


def cmd_list(account: str | None = None) -> int:
    pending = get_pending()
    selected = [item for item in pending if not account or item.get("account") == account]
    if not selected:
        print("No pending articles")
        return 0
    for index, article in enumerate(pending, start=1):
        if article not in selected:
            continue
        print(f"[{index}] {article.get('title', '')}")
        print(f"    id: {article.get('id', '')}")
        print(f"    account: {article.get('account', '')}")
        print(f"    url: {article.get('link', '')}")
    return 0


def cmd_inbox(arguments: argparse.Namespace) -> int:
    result = query_inbox(
        status=arguments.status,
        account=arguments.account or "",
        query=arguments.query or "",
        sort=arguments.sort,
        limit=arguments.limit,
        favorite=arguments.favorite,
        state=arguments.state,
        disposition=arguments.disposition,
    )
    if arguments.format == "json":
        print(json.dumps(result, ensure_ascii=False))
        return 0
    summary = result["summary"]
    print(
        f"Inbox: {summary['pending']} pending, {summary['processed']} processed, "
        f"{summary['sync_pending']} waiting for sync"
    )
    if not result["items"]:
        print("No articles match the current filters")
        return 0
    for item in result["items"]:
        article = item["article"]
        marker = (
            f"pending #{item['pending_index']}"
            if item["status"] == "pending"
            else f"processed / {item.get('sync_status', '')}"
        )
        print(f"- [{marker}] {article.get('title', '')} — {article.get('account', '')}")
        print(f"  {article.get('link', '')}")
    return 0


def cmd_inbox_mark(arguments: argparse.Namespace) -> int:
    favorite: bool | None = None
    if arguments.favorite:
        favorite = True
    elif arguments.unfavorite:
        favorite = False
    state = "later" if arguments.later else ("active" if arguments.active else None)
    result = update_inbox_item(arguments.link, favorite=favorite, state=state)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_dismiss(arguments: argparse.Namespace) -> int:
    entry = dismiss_article(arguments.link)
    print(
        json.dumps(
            {
                "status": "dismissed",
                "reversible": True,
                "article": entry["article"],
                "restore_command": f"process restore --link {entry['article']['link']}",
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_restore(arguments: argparse.Namespace) -> int:
    article = restore_dismissed(arguments.link)
    print(json.dumps({"status": "pending", "article": article}, ensure_ascii=False))
    return 0


def _digest_plan(arguments: argparse.Namespace) -> dict[str, Any]:
    try:
        preferences = load_config()["preferences"]
    except ConfigError:
        preferences = dict(DEFAULT_CONFIG["preferences"])
    hours = arguments.hours if arguments.hours is not None else preferences["digest_hours"]
    limit = arguments.limit if arguments.limit is not None else preferences["digest_limit"]
    return plan_digest(
        preferences,
        hours=hours,
        limit=limit,
        include_later=arguments.include_later,
    )


def cmd_digest_plan(arguments: argparse.Namespace) -> int:
    result = _digest_plan(arguments)
    if arguments.format == "json":
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(
        f"Digest candidates: {result['returned']} of {result['eligible']} eligible "
        f"within {result['window_hours']} hours"
    )
    for index, item in enumerate(result["candidates"], start=1):
        print(f"{index}. {item['title']} — {item['account']}")
        print(f"   {item['url']}")
    return 0


def _configured_request_delay() -> float:
    try:
        return float(load_config()["settings"]["request_delay"])
    except ConfigError:
        return float(DEFAULT_CONFIG["settings"]["request_delay"])


def _print_article(
    article: dict[str, Any],
    *,
    session: Any | None = None,
    pacer: RequestPacer | None = None,
    max_output_chars: int | None = None,
) -> tuple[str, bool]:
    print(f"Title: {article.get('title', '')}")
    print(f"Account: {article.get('account', '')}")
    print(f"URL: {article.get('link', '')}")
    print(f"Digest: {article.get('digest', '')}")
    print("\n--- BEGIN UNTRUSTED ARTICLE CONTENT ---")
    document = fetch_article(str(article["link"]), session=session, pacer=pacer)
    text = str(document["text"])
    record_verified_read(str(article["link"]), text)
    displayed = text if max_output_chars is None else text[:max(0, max_output_chars)]
    print(displayed)
    if len(displayed) < len(text):
        print(f"[Content output truncated: {len(text) - len(displayed)} character(s) omitted]")
    print("--- END UNTRUSTED ARTICLE CONTENT ---")
    suspected = is_advertisement(str(article.get("title", "")), text or "")
    print(f"Ad heuristic: {'suspected' if suspected else 'not detected'}")
    return text, suspected


def cmd_read(arguments: argparse.Namespace) -> int:
    _print_article(
        _resolve(arguments),
        pacer=RequestPacer(_configured_request_delay()),
    )
    return 0


def cmd_batch_read(limit: int) -> int:
    from article_reader import ArticleFetchError, WeChatRiskControlError

    pending = get_pending()
    if not pending:
        print("No pending articles")
        return 0
    requested = min(limit, len(pending))
    successful = 0
    failures: list[Exception] = []
    remaining_output = MAX_BATCH_CONTENT_CHARS
    session = new_session()
    pacer = RequestPacer(_configured_request_delay())
    try:
        for index, article in enumerate(pending[:limit], start=1):
            print(f"\n===== ARTICLE {index}/{requested} =====")
            try:
                text, _ = _print_article(
                    article,
                    session=session,
                    pacer=pacer,
                    max_output_chars=remaining_output,
                )
                remaining_output = max(
                    0, remaining_output - min(len(text), remaining_output)
                )
                successful += 1
            except WeChatRiskControlError as exc:
                raise BatchRiskControlError(article, successful) from exc
            except ArticleFetchError as exc:
                failures.append(exc)
                print(f"[Article read failed: {exc.code}]")
    finally:
        session.close()
    if len(pending) > limit:
        print(f"Stopped at --limit {limit}; {len(pending) - limit} articles remain")
    if failures:
        raise BatchReadError(failures, successful)
    return 0


def cmd_evaluate(arguments: argparse.Namespace) -> int:
    """Fetch one supplied article once and prepare it for Agent scoring."""
    url = canonicalize_wechat_article_url(arguments.url)
    processed = get_processed_entry(url)
    if processed is not None:
        print(
            json.dumps(
                {
                    "status": "already_processed",
                    "article": processed["article"],
                    "metadata": processed["metadata"],
                    "sync_status": processed["sync_status"],
                    "next_action": "none",
                },
                ensure_ascii=False,
            )
        )
        return 0
    document = fetch_article(url)
    text = str(document.get("text", ""))
    article = {
        "title": str(document.get("title", "")) or "Untitled WeChat article",
        "link": str(document["link"]),
        "digest": str(document.get("digest", "")),
        "account": str(document.get("account", "")).strip(),
        "account_id": str(document.get("account_id", "")).strip(),
        "update_time": int(document.get("update_time", 0) or 0),
    }
    try:
        content_dedup = bool(load_config()["settings"]["content_dedup"])
    except ConfigError:
        content_dedup = bool(DEFAULT_CONFIG["settings"]["content_dedup"])
    status, saved = add_pending_with_verified_read(
        article,
        text,
        content_dedup=content_dedup,
    )
    if status == "already_processed":
        result = {
            "status": status,
            "article": saved.get("article", article),
            "metadata": saved.get("metadata", {}),
            "sync_status": saved.get("sync_status", ""),
            "next_action": "none",
        }
    elif status == "duplicate_content":
        result = {
            "status": status,
            "article": article,
            "next_action": "inspect_existing_article",
        }
    else:
        result = {
            "status": status,
            "article": saved,
            "untrusted_article_content": text,
            "ad_heuristic": is_advertisement(article["title"], text),
            "next_action": "score_and_complete_by_url",
        }
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _read_dimensions(arguments: argparse.Namespace) -> Any:
    if arguments.dims_file:
        try:
            # PowerShell 5.1 Out-File -Encoding UTF8 adds a BOM. utf-8-sig
            # accepts both BOM and normal UTF-8 without weakening JSON parsing.
            raw = arguments.dims_file.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ValueError(f"cannot read --dims-file: {exc}") from exc
        source = "--dims-file"
    elif arguments.dims:
        raw = arguments.dims
        source = "--dims"
    else:
        raise ValueError("provide all five dimension scores with --dims or --dims-file")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} is not valid JSON: {exc}") from exc


def _score_metadata(arguments: argparse.Namespace) -> dict[str, Any]:
    dimensions = _read_dimensions(arguments)
    score = calculate_score(dimensions)
    rationale = arguments.rationale or format_rationale(dimensions)
    tags = [item.strip() for item in arguments.tags.split(",") if item.strip()]
    return {
        "score": score,
        "dimensions": dimensions,
        "summary": arguments.summary.strip(),
        "rationale": rationale.strip(),
        "tags": tags,
        "ad": False,
    }


def _sync_entry(entry: dict[str, Any], *, dry_run: bool = False) -> None:
    config = load_config()
    feishu = config["feishu"]
    if not feishu["enabled"]:
        raise ConfigError("Feishu sync is disabled; complete Agent setup first")
    production_feishu_target(feishu).sync(
        entry["article"], entry["metadata"], dry_run=dry_run
    )
    if not dry_run:
        update_sync_status(entry["article"]["link"], "synced")


def _raise_sync_failures(failures: list[Exception], *, prefix: str) -> None:
    """Preserve the first non-retryable failure classification for automation."""
    if not failures:
        return
    primary = next(
        (
            item
            for item in failures
            if not bool(getattr(item, "retryable", False))
        ),
        failures[0],
    )
    message = f"{prefix}; {len(failures)} item(s) remain pending; first failure: {primary}"
    if isinstance(primary, LarkCLIError):
        raise LarkCLIError(
            message,
            kind=primary.kind,
            code=primary.code,
            retryable=all(bool(getattr(item, "retryable", False)) for item in failures),
        ) from primary
    if isinstance(primary, ConfigError):
        raise ConfigError(message) from primary
    raise ValueError(message) from primary


def cmd_done(arguments: argparse.Namespace) -> int:
    if arguments.force_feishu and not arguments.feishu:
        raise ValueError("--force-feishu requires --feishu")
    article = _resolve(arguments)
    if arguments.ad:
        if arguments.dry_run and not arguments.feishu:
            raise ValueError("--dry-run is only valid together with --feishu")
        if arguments.dry_run:
            print(f"Dry run: advertisement remains pending: {article.get('title', '')}")
            return 0
        complete_article(
            article["link"],
            {"ad": True, "reason": "advertisement/promotion"},
            sync_status="skipped_ad",
        )
        print(f"Skipped advertisement: {article.get('title', '')}")
        return 0
    if not has_verified_read(article):
        raise ArticleReadRequiredError()
    try:
        config = load_config()
    except ConfigError:
        if arguments.feishu:
            raise
        config = None
    if arguments.dry_run and not arguments.feishu:
        raise ValueError("--dry-run is only valid together with --feishu")
    metadata = _score_metadata(arguments)
    sync_requested = bool(arguments.feishu)
    if sync_requested:
        if config is None:
            raise ConfigError("Feishu sync requires configuration")
        if arguments.force_feishu or should_sync(
            metadata["score"], config["settings"]["min_score"]
        ):
            status = "pending"
        else:
            status = "skipped_low_score"
    else:
        status = "not_requested"
    if arguments.dry_run:
        if status != "pending":
            print(
                f"Dry run: score {metadata['score']} is below the configured Feishu threshold"
            )
            return 0
        _sync_entry({"article": article, "metadata": metadata}, dry_run=True)
        print(f"Dry run succeeded; article remains pending: {article.get('title', '')}")
        return 0
    entry = complete_article(article["link"], metadata, sync_status=status)
    if entry.get("metadata", {}).get("disposition") == "dismissed":
        raise LookupError("article was dismissed and cannot be completed")
    if status == "pending":
        try:
            _sync_entry(entry, dry_run=arguments.dry_run)
        except (ConfigError, LarkCLIError, ValueError) as exc:
            if not arguments.dry_run:
                update_sync_status(article["link"], "pending", str(exc))
            _raise_sync_failures(
                [exc],
                prefix="article was saved locally but Feishu sync failed",
            )
    print(f"Completed: {article.get('title', '')} (score {metadata['score']})")
    return 0


def cmd_sync_all(*, dry_run: bool = False) -> int:
    if not dry_run:
        raise ValueError(
            "bulk Feishu sync is preview-only; retry each explicitly confirmed "
            "article with sync-feishu --link"
        )
    entries = pending_sync_entries()
    if not entries:
        print("No articles are waiting for Feishu sync")
        return 0
    failures: list[Exception] = []
    for entry in entries:
        try:
            _sync_entry(entry, dry_run=dry_run)
            print(f"Synced: {entry['article'].get('title', '')}")
        except (ConfigError, LarkCLIError, ValueError) as exc:
            failures.append(exc)
            if not dry_run:
                update_sync_status(entry["article"]["link"], "pending", str(exc))
            print(f"Sync failed: {entry['article'].get('title', '')}: {exc}")
    _raise_sync_failures(failures, prefix="one or more Feishu sync operations failed")
    return 0


def cmd_sync_one(link: str, *, dry_run: bool = False, force_feishu: bool = False) -> int:
    entry = get_processed_entry(link)
    if entry is None:
        raise LookupError("no processed article matches that URL")
    metadata = entry.get("metadata", {})
    if metadata.get("disposition") == "dismissed" or metadata.get("ad"):
        raise ValueError("dismissed or advertisement articles cannot be synced to Feishu")
    if entry.get("sync_status") == "synced":
        print(f"Already synced: {entry['article'].get('title', '')}")
        return 0
    score = metadata.get("score")
    if not isinstance(score, (int, float)):
        raise ValueError("processed article has no valid score to sync")
    config = load_config()
    if not force_feishu and not should_sync(float(score), config["settings"]["min_score"]):
        raise ValueError(
            "article score is below the configured Feishu threshold; "
            "use --force-feishu only after explicit per-article confirmation"
        )
    try:
        _sync_entry(entry, dry_run=dry_run)
    except (ConfigError, LarkCLIError, ValueError) as exc:
        if not dry_run:
            update_sync_status(entry["article"]["link"], "pending", str(exc))
        _raise_sync_failures(
            [exc],
            prefix="processed article remains local because Feishu sync failed",
        )
    action = "Dry run succeeded" if dry_run else "Synced"
    print(f"{action}: {entry['article'].get('title', '')}")
    return 0


def cmd_feishu_check(*, save_mapping: bool = False) -> int:
    config = load_config()
    try:
        check = production_feishu_target(config["feishu"]).check()
    except Exception as exc:
        try:
            update_health(
                "feishu",
                success=False,
                failure_kind=getattr(exc, "kind", type(exc).__name__),
            )
        except ConfigError:
            pass
        raise
    if save_mapping:
        def mutate_mapping(config: dict[str, Any]) -> dict[str, Any]:
            config["feishu"]["field_mapping"] = check["mapping"]
            return config

        config = modify_config(mutate_mapping)
    update_health("feishu", success=True)
    print(
        json.dumps(
            {
                "ok": True,
                "identity": check["identity"],
                "field_count": check["field_count"],
                "field_mapping": check["mapping"],
                "mapping_saved": save_mapping,
                "note": "Read-only checks passed. A real write requires the explicit --feishu flag.",
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_feishu_schema() -> int:
    print(json.dumps(standard_field_schema(), ensure_ascii=False))
    return 0


def _add_selector(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("index", type=int, nargs="?", help="1-based pending index")
    parser.add_argument("--link", help="stable article URL; preferred for automation")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    commands = parser.add_subparsers(dest="command", required=True)
    list_parser = commands.add_parser("list")
    list_parser.add_argument("--account")
    inbox_parser = commands.add_parser("inbox")
    inbox_parser.add_argument("--status", choices=("pending", "processed", "all"), default="pending")
    inbox_parser.add_argument("--account")
    inbox_parser.add_argument("--query")
    inbox_parser.add_argument("--sort", choices=("newest", "oldest"), default="newest")
    inbox_parser.add_argument("--limit", type=int, default=20)
    inbox_parser.add_argument("--favorite", action="store_true")
    inbox_parser.add_argument("--state", choices=("active", "later", "all"), default="all")
    inbox_parser.add_argument(
        "--disposition",
        choices=("completed", "dismissed", "all"),
        default="all",
    )
    mark_parser = commands.add_parser("inbox-mark")
    mark_parser.add_argument("--link", required=True)
    favorite_choice = mark_parser.add_mutually_exclusive_group()
    favorite_choice.add_argument("--favorite", action="store_true")
    favorite_choice.add_argument("--unfavorite", action="store_true")
    state_choice = mark_parser.add_mutually_exclusive_group()
    state_choice.add_argument("--later", action="store_true")
    state_choice.add_argument("--active", action="store_true")
    dismiss_parser = commands.add_parser("dismiss")
    dismiss_parser.add_argument("--link", required=True)
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("--link", required=True)
    digest_parser = commands.add_parser("digest-plan")
    digest_parser.add_argument("--hours", type=int)
    digest_parser.add_argument("--limit", type=int)
    digest_parser.add_argument("--include-later", action="store_true")
    read_parser = commands.add_parser("read")
    _add_selector(read_parser)
    batch_parser = commands.add_parser("batch-read")
    batch_parser.add_argument("--limit", type=int, default=10)
    evaluate_parser = commands.add_parser("evaluate")
    evaluate_parser.add_argument(
        "--url",
        required=True,
        help="WeChat article URL to read and prepare for scoring without discovery",
    )
    done_parser = commands.add_parser("done")
    _add_selector(done_parser)
    done_parser.add_argument("--ad", action="store_true")
    dimensions = done_parser.add_mutually_exclusive_group()
    dimensions.add_argument("--dims", help="JSON object containing exactly five dimensions")
    dimensions.add_argument(
        "--dims-file",
        type=Path,
        help="UTF-8 JSON file containing exactly five dimensions",
    )
    done_parser.add_argument("--summary", default="")
    done_parser.add_argument("--rationale", default="")
    done_parser.add_argument("--tags", default="")
    done_parser.add_argument("--feishu", action="store_true")
    done_parser.add_argument(
        "--force-feishu",
        action="store_true",
        help="honor an explicit single-article write request even below the score threshold",
    )
    done_parser.add_argument("--dry-run", action="store_true")
    sync_parser = commands.add_parser("sync-feishu")
    sync_selector = sync_parser.add_mutually_exclusive_group(required=True)
    sync_selector.add_argument("--all", action="store_true")
    sync_selector.add_argument("--link")
    sync_parser.add_argument("--force-feishu", action="store_true")
    sync_parser.add_argument("--dry-run", action="store_true")
    check_parser = commands.add_parser("feishu-check")
    check_parser.add_argument("--save-mapping", action="store_true")
    commands.add_parser("feishu-schema")
    export_parser = commands.add_parser("export")
    export_parser.add_argument("path", type=Path)
    clean_parser = commands.add_parser("clean")
    clean_parser.add_argument("--days", type=int, default=365)
    clean_parser.add_argument("--yes", action="store_true")
    return parser


def _dispatch(arguments: argparse.Namespace) -> int:
    if arguments.command == "list":
        return cmd_list(arguments.account)
    if arguments.command == "inbox":
        if arguments.limit < 1 or arguments.limit > 100:
            raise ValueError("--limit must be between 1 and 100")
        return cmd_inbox(arguments)
    if arguments.command == "inbox-mark":
        if not any(
            (arguments.favorite, arguments.unfavorite, arguments.later, arguments.active)
        ):
            raise ValueError("choose favorite/unfavorite and/or later/active")
        return cmd_inbox_mark(arguments)
    if arguments.command == "dismiss":
        return cmd_dismiss(arguments)
    if arguments.command == "restore":
        return cmd_restore(arguments)
    if arguments.command == "digest-plan":
        return cmd_digest_plan(arguments)
    if arguments.command == "read":
        return cmd_read(arguments)
    if arguments.command == "batch-read":
        if arguments.limit < 1 or arguments.limit > 100:
            raise ValueError("--limit must be between 1 and 100")
        return cmd_batch_read(arguments.limit)
    if arguments.command == "evaluate":
        return cmd_evaluate(arguments)
    if arguments.command == "done":
        if arguments.index is None and not arguments.link:
            raise ValueError("provide an index or --link")
        return cmd_done(arguments)
    if arguments.command == "sync-feishu":
        if arguments.force_feishu and not arguments.link:
            raise ValueError("--force-feishu requires --link")
        if arguments.link:
            return cmd_sync_one(
                arguments.link,
                dry_run=arguments.dry_run,
                force_feishu=arguments.force_feishu,
            )
        return cmd_sync_all(dry_run=arguments.dry_run)
    if arguments.command == "feishu-check":
        return cmd_feishu_check(save_mapping=arguments.save_mapping)
    if arguments.command == "feishu-schema":
        return cmd_feishu_schema()
    if arguments.command == "export":
        print(export_queue(arguments.path))
        return 0
    if arguments.command == "clean":
        candidates = cleanup_processed(arguments.days, dry_run=not arguments.yes)
        if not arguments.yes:
            print(
                f"Preview: {candidates} old record(s) would be permanently removed; "
                "rerun with --yes to confirm"
            )
            return 0
        print(f"Removed {candidates} old records")
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    json_output = arguments.format == "json"
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output) if json_output else contextlib.nullcontext():
            result = _dispatch(arguments)
        if json_output:
            lines = [line for line in output.getvalue().splitlines() if line.strip()]
            command_data: Any = {"command": arguments.command, "output": lines}
            if arguments.command in {
                "evaluate",
                "inbox",
                "inbox-mark",
                "dismiss",
                "restore",
                "digest-plan",
            } and len(lines) == 1:
                try:
                    command_data = json.loads(lines[0])
                except json.JSONDecodeError:
                    pass
            next_action = "none" if result == 0 else "inspect_failed_items"
            if (
                arguments.command == "digest-plan"
                and isinstance(command_data, dict)
                and command_data.get("candidates")
            ):
                next_action = "read_score_digest_candidates"
            envelope = success(
                command_data,
                next_action=next_action,
            )
            if result:
                envelope["ok"] = False
                envelope["error"] = {
                    "code": "COMMAND_PARTIAL_FAILURE",
                    "message": "one or more items failed",
                    "retryable": True,
                    "next_action": "inspect_failed_items",
                }
            print(dump(envelope))
        return result
    except (ConfigError, LarkCLIError, LookupError, ValueError) as exc:
        if json_output:
            print(dump(failure(exc)))
        else:
            logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
