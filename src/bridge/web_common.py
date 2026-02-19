"""Shared helpers for web execution modules."""

from __future__ import annotations

import importlib.util
from urllib.parse import urlparse


def collapse_ws(value: object) -> str:
    return " ".join(str(value or "").split())


def is_generic_play_label(value: str) -> bool:
    low = str(value or "").strip().lower()
    return low in {"reproducir", "play", "play local"}


def normalize_url(raw: str) -> str:
    return raw.rstrip(".,;:!?)]}\"'")


def is_valid_url(text: str) -> bool:
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def same_origin_path(current_url: str, target_url: str) -> bool:
    try:
        current = urlparse(current_url)
        target = urlparse(target_url)
    except ValueError:
        return False
    if not current.scheme or not current.netloc:
        return False
    return (
        current.scheme == target.scheme
        and current.netloc == target.netloc
        and (current.path or "/") == (target.path or "/")
    )


def playwright_available() -> bool:
    return importlib.util.find_spec("playwright.sync_api") is not None


def safe_page_title(page: object) -> str:
    title_attr = getattr(page, "title", None)
    if not callable(title_attr):
        return ""
    try:
        value = title_attr()
    except Exception:
        return ""
    return str(value or "")
