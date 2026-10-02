"""Article inbox queries over the local queue."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from queue_helpers import read_queue


def _timestamp(item: dict[str, Any]) -> float:
    article = item["article"]
    try:
        published = float(article.get("update_time") or 0)
    except (TypeError, ValueError):
        published = 0
    if published:
        return published
    for key in ("processed_at", "discovered_at"):
        value = item.get(key) or article.get(key)
        if value:
            try:
                return datetime.fromisoformat(str(value)).timestamp()
            except ValueError:
                continue
    return 0


def _item_matches(
    item: dict[str, Any],
    *,
    account: str,
    query: str,
    disposition: str,
) -> bool:
    article = item["article"]
    if (
        item["status"] == "processed"
        and disposition != "all"
        and item["disposition"] != disposition
    ):
        return False
    if account and str(article.get("account", "")).strip().casefold() != account:
        return False
    searchable = " ".join(
        [
            str(article.get("title", "")),
            str(article.get("account", "")),
            str(article.get("digest", "")),
            str(item.get("summary", "")),
            " ".join(str(tag) for tag in item.get("tags", [])),
        ]
    ).casefold()
    return not query or query in searchable


def _queue_summary(queue: dict[str, Any]) -> dict[str, Any]:
    """One summary implementation shared by the inbox view and callers."""
    return {
        "pending": len(queue["pending"]),
        "processed": len(queue["processed"]),
        "dismissed": sum(
            entry.get("metadata", {}).get("disposition") == "dismissed"
            for entry in queue["processed"].values()
            if isinstance(entry, dict)
        ),
        "sync_pending": sum(
            entry.get("sync_status") == "pending"
            for entry in queue["processed"].values()
            if isinstance(entry, dict)
        ),
    }


def queue_summary() -> dict[str, Any]:
    """Return one consistent summary of pending and processed articles."""
    return _queue_summary(read_queue())


def query_inbox(
    *,
    status: str = "pending",
    account: str = "",
    query: str = "",
    sort: str = "newest",
    limit: int = 20,
    disposition: str = "all",
) -> dict[str, Any]:
    """Return one stable, filtered view of pending and processed articles."""
    if status not in {"pending", "processed", "all"}:
        raise ValueError("status must be pending, processed, or all")
    if sort not in {"newest", "oldest"}:
        raise ValueError("sort must be newest or oldest")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if disposition not in {"completed", "dismissed", "all"}:
        raise ValueError("disposition must be completed, dismissed, or all")

    queue = read_queue()
    items: list[dict[str, Any]] = []
    if status in {"pending", "all"}:
        items.extend(
            {
                "status": "pending",
                "pending_index": index,
                "article": article,
                "discovered_at": article.get("discovered_at", ""),
            }
            for index, article in enumerate(queue["pending"], start=1)
        )
    if status in {"processed", "all"}:
        items.extend(
            {
                "status": "processed",
                "article": entry["article"],
                "processed_at": entry.get("processed_at", ""),
                "sync_status": entry.get("sync_status", ""),
                "score": entry.get("metadata", {}).get("score"),
                "summary": entry.get("metadata", {}).get("summary", ""),
                "tags": entry.get("metadata", {}).get("tags", []),
                "disposition": str(entry.get("metadata", {}).get("disposition", "completed")),
            }
            for entry in queue["processed"].values()
            if isinstance(entry, dict) and isinstance(entry.get("article"), dict)
        )

    normalized_account = " ".join(account.split()).casefold()
    normalized_query = " ".join(query.split()).casefold()
    selected = [
        item
        for item in items
        if _item_matches(
            item,
            account=normalized_account,
            query=normalized_query,
            disposition=disposition,
        )
    ]
    selected.sort(key=_timestamp, reverse=sort == "newest")
    matched = len(selected)
    selected = selected[:limit]
    return {
        "summary": {**_queue_summary(queue), "matched": matched, "returned": len(selected)},
        "filters": {
            "status": status,
            "account": account,
            "query": query,
            "sort": sort,
            "limit": limit,
            "disposition": disposition,
        },
        "items": selected,
    }


