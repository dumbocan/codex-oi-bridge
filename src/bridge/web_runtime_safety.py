"""Safety and runtime utility helpers for web execution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from bridge.web_session import WebSession, request_session_state


def _observer_noise_mode() -> str:
    raw = str(os.getenv("BRIDGE_OBSERVER_NOISE_MODE", "minimal")).strip().lower()
    return "debug" if raw == "debug" else "minimal"


def page_is_closed(page: Any | None) -> bool:
    if page is None:
        return True
    checker = getattr(page, "is_closed", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return True
    return False


def is_page_closed_error(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    return (
        "target page" in msg and "closed" in msg
    ) or "context or browser has been closed" in msg or "page closed" in msg


def runtime_closed(page: Any | None, session: WebSession | None) -> bool:
    if page_is_closed(page):
        return True
    if session is None:
        return False
    return str(getattr(session, "state", "open")).strip().lower() == "closed"


def observer_useful_event_count(session: WebSession | None) -> int:
    if session is None:
        return 0
    try:
        state = request_session_state(session)
    except BaseException:
        return 0
    events = list(state.get("recent_events", []) or [])
    noise_mode = str(state.get("observer_noise_mode", _observer_noise_mode())).strip().lower()
    useful_types = {"click", "network_warn", "network_error", "console_error", "page_error"}
    if noise_mode == "debug":
        useful_types.update({"scroll", "mousemove"})
    count = 0
    for evt in events:
        if not isinstance(evt, dict):
            continue
        etype = str(evt.get("type", "")).strip().lower()
        if etype in useful_types:
            count += 1
    return count


def capture_timeout_evidence(
    *,
    page: Any,
    evidence_dir: Path,
    evidence_paths: list[str],
    name: str,
) -> None:
    timeout_path = evidence_dir / name
    try:
        page.screenshot(path=str(timeout_path), full_page=False)
        evidence_paths.append(to_repo_rel(timeout_path))
    except Exception:
        pass


def to_repo_rel(path: Path) -> str:
    return str(path.resolve().relative_to(Path.cwd()))
