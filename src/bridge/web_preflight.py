"""Preflight/navigation helpers for web runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class PreflightResult:
    learning_context: dict[str, str]
    control_enabled: bool


def execute_preflight(
    *,
    page: Any,
    url: str,
    visual: bool,
    visual_cursor: bool,
    overlay_debug_path: Path,
    evidence_dir: Path,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    evidence_paths: list[str],
    attached: bool,
    session: Any | None,
    control_enabled: bool,
    learning_context_fn: Callable[[str, str], dict[str, str]],
    safe_page_title: Callable[[Any], str],
    same_origin_path: Callable[[str, str], bool],
    ensure_visual_overlay_ready: Callable[..., None],
    set_assistant_control_overlay: Callable[[Any, bool], None],
    update_top_bar_state: Callable[[Any, dict[str, Any]], None],
    session_state_payload: Callable[..., dict[str, Any]],
    mark_controlled: Callable[..., None],
    to_repo_rel: Callable[[Path], str],
    collapse_ws: Callable[[str], str],
) -> PreflightResult:
    learning_context = learning_context_fn(url, "")
    initial_url = page.url
    initial_title = safe_page_title(page)
    learning_context = learning_context_fn(url, initial_title)
    observations.append(f"Initial url/title: {initial_url} | {initial_title}")
    target_matches = same_origin_path(initial_url, url)
    if target_matches:
        observations.append("Navigation skipped (already at target)")
    else:
        actions.append(f"cmd: playwright goto {url}")
        page.goto(url, wait_until="domcontentloaded")
        observations.append(f"Opened URL: {url}")
        if visual:
            ensure_visual_overlay_ready(
                page,
                ui_findings,
                cursor_expected=visual_cursor,
                retries=3,
                delay_ms=140,
                debug_screenshot_path=overlay_debug_path,
                force_reinit=True,
            )

    if visual:
        ensure_visual_overlay_ready(
            page,
            ui_findings,
            cursor_expected=visual_cursor,
            retries=3,
            delay_ms=140,
            debug_screenshot_path=overlay_debug_path,
            force_reinit=True,
        )
        set_assistant_control_overlay(page, True)
        control_enabled = True
        update_top_bar_state(
            page,
            session_state_payload(session, override_controlled=True),
        )
    observations.append(f"Page title: {safe_page_title(page)}")
    if attached and session is not None:
        mark_controlled(session, True, url=page.url, title=safe_page_title(page))

    try:
        context_path = evidence_dir / "step_0_context.png"
        page.screenshot(path=str(context_path), full_page=True)
        evidence_paths.append(to_repo_rel(context_path))
    except Exception:
        pass
    try:
        body_text = page.evaluate(
            "() => (document.body && document.body.innerText ? document.body.innerText.slice(0, 500) : '')"
        )
    except Exception:
        body_text = ""
    body_snippet = collapse_ws(str(body_text or ""))[:500]
    ui_findings.append(
        f"context title={safe_page_title(page)} url={page.url} body[:500]={body_snippet}"
    )
    return PreflightResult(
        learning_context=learning_context,
        control_enabled=control_enabled,
    )
