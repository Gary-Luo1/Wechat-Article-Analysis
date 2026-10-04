"""Ephemeral article text kept only until a pending review is finished.

The queue stores a content hash, not the body. This cache lets a later
``evaluate`` or ``content`` call reuse the text without another WeChat fetch.
It is deleted when the article is completed or dismissed, and exports never
include it.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from paths import data_dir
from url_identity import normalize_article_url


# Long enough for a typical article, short enough to keep one tool result usable.
INLINE_CONTENT_CHARS = 24_000


def article_cache_dir() -> Path:
    return data_dir() / "article-cache"


def _cache_path(link: str) -> Path:
    normalized = normalize_article_url(link)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return article_cache_dir() / digest


def store_article_text(link: str, text: str) -> str:
    """Persist one article body and return its SHA-256 hex digest."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("article text must be non-empty")
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path = _cache_path(link)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".article.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        if os.name != "nt":
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if os.name != "nt":
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            raise
    return fingerprint


def load_article_text(link: str) -> str | None:
    """Return cached text, or None when this link has no cache file."""
    path = _cache_path(link)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text or None


def delete_article_text(link: str) -> None:
    """Remove one cached body. Missing files are ignored."""
    try:
        _cache_path(link).unlink(missing_ok=True)
    except OSError:
        return


def clear_article_cache() -> None:
    """Remove every ephemeral body. The queue file is left untouched."""
    root = article_cache_dir()
    if not root.exists():
        return
    for child in root.iterdir():
        if child.is_file():
            try:
                child.unlink()
            except OSError:
                continue
    try:
        root.rmdir()
    except OSError:
        return


def inline_article_text(text: str) -> tuple[str, bool]:
    """Return a bounded tool-result excerpt and whether it was truncated."""
    if len(text) <= INLINE_CONTENT_CHARS:
        return text, False
    return text[:INLINE_CONTENT_CHARS], True
