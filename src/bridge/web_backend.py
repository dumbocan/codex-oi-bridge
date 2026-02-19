"""Deterministic web interaction backend using Playwright."""

from __future__ import annotations

import json
import re
import socket
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from bridge.models import OIReport
from bridge.web_common import (
    collapse_ws as _collapse_ws,
    is_generic_play_label as _is_generic_play_label,
    is_valid_url as _is_valid_url,
    normalize_url as _normalize_url,
    playwright_available as _playwright_available,
    safe_page_title as _safe_page_title,
    same_origin_path as _same_origin_path,
)
from bridge.web_overlay import (
    destroy_top_bar as _destroy_top_bar,
    notify_learning_state as _notify_learning_state,
    session_state_payload as _session_state_payload,
    set_assistant_control_overlay as _set_assistant_control_overlay,
    set_learning_handoff_overlay as _set_learning_handoff_overlay,
    set_user_control_overlay as _set_user_control_overlay,
    update_top_bar_state as _update_top_bar_state,
)
from bridge.web_bulk_scan import (
    scan_visible_buttons_in_cards as _bulk_scan_visible_buttons_in_cards,
    scan_visible_selectors as _bulk_scan_visible_selectors,
)
from bridge.web_executor_steps import (
    INTERACTIVE_STEP_KINDS,
    TEACHING_HANDOFF_KINDS,
    append_iframe_focus_findings,
    append_interactive_timeout_findings,
    append_run_crash_findings,
    append_wait_timeout_findings,
    step_learning_target as _step_learning_target,
)
from bridge.web_frame_guard import (
    disable_active_youtube_iframe_pointer_events as _frame_disable_active_youtube_iframe_pointer_events,
    force_main_frame_context as _frame_force_main_frame_context,
    is_iframe_focus_locked as _frame_is_iframe_focus_locked,
    restore_iframe_pointer_events as _frame_restore_iframe_pointer_events,
)
from bridge.web_handoff import (
    show_custom_handoff_notice as _handoff_show_custom_notice,
    show_stuck_handoff_notice as _handoff_show_stuck_notice,
    trigger_stuck_handoff as _handoff_trigger_stuck,
)
from bridge.web_interaction_helpers import (
    apply_wait_step as _helpers_apply_wait_step,
    retry_scroll as _retry_scroll,
    semantic_hints_for_selector as _semantic_hints_for_selector,
    stable_selectors_for_target as _stable_selectors_for_target,
)
from bridge.web_interactive_capture import (
    capture_movement as _capture_movement_artifact,
)
from bridge.web_mouse import (
    _human_mouse_click,
    _human_mouse_move,
    get_last_human_route as _get_last_human_route,
)
from bridge.web_run_finalize import (
    finalize_result as _finalize_result,
)
from bridge.storage import write_json, write_status
from bridge.web_teaching import (
    capture_manual_learning as _teaching_capture_manual_learning,
    is_relevant_manual_learning_event as _teaching_is_relevant_manual_learning_event,
    normalize_failed_target_label as _teaching_normalize_failed_target_label,
    resume_after_learning as _teaching_resume_after_learning,
    show_learning_thanks_notice as _teaching_show_learning_thanks_notice,
    show_teaching_handoff_notice as _teaching_show_handoff_notice,
    show_wrong_manual_click_notice as _teaching_show_wrong_click_notice,
    write_teaching_artifacts as _teaching_write_artifacts,
)
from bridge.web_watchdog import (
    WebWatchdogConfig,
    WebWatchdogState,
    evaluate_stuck_reason,
    poll_progress as watchdog_poll_progress,
    remaining_ms as watchdog_remaining_ms,
    update_step_signature,
)
from bridge.web_visual_overlay import (
    _ensure_visual_overlay_installed,
    _highlight_target,
    _install_visual_overlay,
    _read_visual_overlay_snapshot,
    _verify_visual_overlay_visible,
)
from bridge.web_steps import (
    WebStep,
    parse_steps,
)
from bridge.web_session import (
    WebSession,
    mark_controlled,
    request_session_state,
)

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")

_LEARNING_DIR = Path("runs") / "learning"
_LEARNING_JSON = _LEARNING_DIR / "web_teaching_selectors.json"


def _observer_noise_mode() -> str:
    raw = str(os.getenv("BRIDGE_OBSERVER_NOISE_MODE", "minimal")).strip().lower()
    return "debug" if raw == "debug" else "minimal"


def run_web_task(
    task: str,
    run_dir: Path,
    timeout_seconds: int,
    verified: bool = False,
    progress_cb: Callable[[int, int, str], None] | None = None,
    visual: bool = False,
    visual_cursor: bool = True,
    visual_click_pulse: bool = True,
    visual_scale: float = 1.0,
    visual_color: str = "#3BA7FF",
    visual_human_mouse: bool = True,
    visual_mouse_speed: float = 1.0,
    visual_click_hold_ms: int = 180,
    session: WebSession | None = None,
    keep_open: bool = False,
    teaching_mode: bool = False,
) -> OIReport:
    url_match = _URL_RE.search(task)
    if not url_match:
        raise SystemExit("Web mode requires an explicit URL in task.")
    url = _normalize_url(url_match.group(0))
    if not _is_valid_url(url):
        raise SystemExit(f"Web mode received invalid URL token: {url_match.group(0)}")
    _preflight_target_reachable(url)
    _preflight_stack_prereqs()
    steps = _parse_steps(task)

    if not _playwright_available():
        raise SystemExit(
            "Playwright Python package is not installed. "
            "Install it in the environment to use --mode web."
        )

    report: OIReport | None = None
    try:
        report = _execute_playwright(
            url,
            steps,
            run_dir,
            timeout_seconds,
            verified=verified,
            progress_cb=progress_cb,
            visual=visual,
            visual_cursor=visual_cursor,
            visual_click_pulse=visual_click_pulse,
            visual_scale=visual_scale,
            visual_color=visual_color,
            visual_human_mouse=visual_human_mouse,
            visual_mouse_speed=visual_mouse_speed,
            visual_click_hold_ms=visual_click_hold_ms,
            session=session,
            keep_open=keep_open,
            teaching_mode=teaching_mode,
        )
    except BaseException as exc:
        msg = str(exc) or exc.__class__.__name__
        report = OIReport(
            task_id=run_dir.name,
            goal=f"web: {url}",
            actions=[],
            observations=["web executor aborted before completion"],
            console_errors=[f"Unhandled web execution error: {msg}"],
            network_findings=[],
            ui_findings=[
                "what_failed=run_crash",
                "where=web-run",
                f"why_likely={msg}",
                "attempted=executor run",
                "next_best_action=inspect logs and retry",
                "final_state=failed",
            ],
            result="failed",
            evidence_paths=[],
        )
    finally:
        if report is not None:
            try:
                write_json(run_dir / "report.json", report.to_dict())
            except Exception:
                pass
            try:
                write_status(
                    run_id=run_dir.name,
                    run_dir=run_dir,
                    task=task,
                    result=report.result,
                    state="completed",
                    report_path=run_dir / "report.json",
                    progress="web run finalized",
                )
            except Exception:
                pass
    return report


def _preflight_target_reachable(url: str, timeout_seconds: float = 1.2) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if not host or port <= 0:
        raise SystemExit(f"Web target not reachable: {url}")

    # Many dev servers bind IPv4 only; prefer 127.0.0.1 first for localhost.
    candidates: list[str] = [host]
    if host in {"localhost", "0.0.0.0"}:
        candidates = ["127.0.0.1", "localhost", "::1"]

    last_exc: Exception | None = None
    for cand in candidates:
        try:
            with socket.create_connection((cand, int(port)), timeout=timeout_seconds):
                return
        except Exception as exc:  # pragma: no cover (covered via raised SystemExit)
            last_exc = exc
            continue
    raise SystemExit(f"Web target not reachable: {url}") from last_exc


def _http_quick_check(url: str, timeout_seconds: float = 1.2) -> None:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        if int(resp.status) < 200 or int(resp.status) >= 400:
            raise SystemExit(f"Start your stack first: {url} returned {resp.status}")


def _preflight_stack_prereqs() -> None:
    # Optional project-specific stack preflight. Enabled via env to avoid hardcoding.
    # Example:
    #   export BRIDGE_WEB_PREFLIGHT_STACK=1
    #   export BRIDGE_WEB_PREFLIGHT_BACKEND_HEALTH_URL=http://127.0.0.1:8010/health
    #   export BRIDGE_WEB_PREFLIGHT_FRONTEND_URL=http://127.0.0.1:5181/
    stack_enabled = os.getenv("BRIDGE_WEB_PREFLIGHT_STACK", "0").strip() == "1"
    backend = os.getenv("BRIDGE_WEB_PREFLIGHT_BACKEND_HEALTH_URL", "").strip()
    frontend = os.getenv("BRIDGE_WEB_PREFLIGHT_FRONTEND_URL", "").strip()
    if stack_enabled and not backend:
        backend = "http://127.0.0.1:8010/health"
    if stack_enabled and not frontend:
        frontend = "http://127.0.0.1:5181/"
    if not backend and not frontend:
        return
    try:
        if backend:
            _http_quick_check(backend)
        if frontend:
            _http_quick_check(frontend)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit("Start your stack first") from exc


def release_session_control_overlay(session: WebSession) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{session.port}")
        except Exception:
            return
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            return
        page = context.pages[0] if context.pages else None
        if page is None:
            return
        try:
            _set_assistant_control_overlay(page, False)
            _update_top_bar_state(page, _session_state_payload(session, override_controlled=False))
        except Exception:
            return


def destroy_session_top_bar(session: WebSession) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{session.port}")
        except Exception:
            return
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            return
        page = context.pages[0] if context.pages else None
        if page is None:
            return
        try:
            _destroy_top_bar(page)
        except Exception:
            return


def ensure_session_top_bar(session: WebSession) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{session.port}")
        except Exception:
            return
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        try:
            _install_visual_overlay(
                page,
                cursor_enabled=False,
                click_pulse_enabled=False,
                scale=1.0,
                color="#3BA7FF",
                trace_enabled=False,
                session_state=_session_state_payload(session),
            )
            _set_assistant_control_overlay(page, bool(session.controlled))
            _update_top_bar_state(page, _session_state_payload(session))
        except Exception:
            return


def _parse_steps(task: str) -> list[WebStep]:
    return parse_steps(task)


def _page_is_closed(page: Any | None) -> bool:
    if page is None:
        return True
    checker = getattr(page, "is_closed", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return True
    return False


def _is_page_closed_error(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    return (
        "target page" in msg and "closed" in msg
    ) or "context or browser has been closed" in msg or "page closed" in msg


def _runtime_closed(page: Any | None, session: WebSession | None) -> bool:
    if _page_is_closed(page):
        return True
    if session is None:
        return False
    return str(getattr(session, "state", "open")).strip().lower() == "closed"


def _execute_playwright(
    url: str,
    steps: list[WebStep],
    run_dir: Path,
    timeout_seconds: int,
    *,
    verified: bool,
    progress_cb: Callable[[int, int, str], None] | None = None,
    visual: bool = False,
    visual_cursor: bool = True,
    visual_click_pulse: bool = True,
    visual_scale: float = 1.0,
    visual_color: str = "#3BA7FF",
    visual_human_mouse: bool = True,
    visual_mouse_speed: float = 1.0,
    visual_click_hold_ms: int = 180,
    session: WebSession | None = None,
    keep_open: bool = False,
    teaching_mode: bool = False,
) -> OIReport:
    from playwright.sync_api import sync_playwright

    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    actions: list[str] = []
    if visual:
        actions.append("cmd: playwright visual on")
    observations: list[str] = []
    console_errors: list[str] = []
    network_findings: list[str] = []
    ui_findings: list[str] = []
    evidence_paths: list[str] = []
    learning_notes: list[str] = []
    learning_context: dict[str, str] = {}
    force_keep_open = False
    release_for_handoff = False
    wait_for_human_learning = False
    handoff_reason = ""
    handoff_where = ""
    handoff_attempted = ""
    failed_target_for_teaching = ""
    learning_iframe_guard: dict[str, Any] | None = None
    page = None
    interactive_timeout_ms = 8000
    learned_selector_map = _load_learned_selectors()
    watchdog_state = WebWatchdogState()
    step_hard_timeout_seconds = max(
        0.1, float(os.getenv("BRIDGE_WEB_STEP_HARD_TIMEOUT_SECONDS", "20") or "20")
    )
    run_hard_timeout_seconds = max(
        0.1, float(os.getenv("BRIDGE_WEB_RUN_HARD_TIMEOUT_SECONDS", "120") or "120")
    )
    run_started_at = time.monotonic()
    run_deadline_ts = run_started_at + run_hard_timeout_seconds

    with sync_playwright() as p:
        browser = None
        page = None
        context = None
        attached = session is not None
        if attached:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{session.port}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            mark_controlled(session, True, url=page.url, title=_safe_page_title(page))
        else:
            browser = _launch_browser(
                p,
                visual=visual,
                visual_mouse_speed=visual_mouse_speed,
            )
            page = browser.new_page()
        page.set_default_timeout(min(timeout_seconds * 1000, 120000))
        if visual:
            overlay_debug_path = evidence_dir / "step_overlay_debug.png"
            try:
                _install_visual_overlay(
                    page,
                    cursor_enabled=visual_cursor,
                    click_pulse_enabled=visual_click_pulse,
                    scale=visual_scale,
                    color=visual_color,
                    trace_enabled=False,
                    session_state=_session_state_payload(session),
                )
                page.bring_to_front()
            except Exception as exc:
                ui_findings.append(f"visual overlay install failed; degraded mode: {exc}")

            if attached:
                _ensure_visual_overlay_ready_best_effort(
                    page,
                    ui_findings,
                    cursor_expected=visual_cursor,
                    retries=3,
                    delay_ms=140,
                    debug_screenshot_path=overlay_debug_path,
                    force_reinit=True,
                )

        def on_console(msg: Any) -> None:
            if msg.type == "error":
                console_errors.append(msg.text)

        def on_response(resp: Any) -> None:
            try:
                if resp.status >= 400:
                    network_findings.append(f"{resp.request.method} {resp.url} {resp.status}")
            except Exception:
                pass

        def on_failed(req: Any) -> None:
            failure = req.failure
            text = failure.get("errorText") if isinstance(failure, dict) else str(failure)
            network_findings.append(f"FAILED {req.method} {req.url} {text}")

        page.on("console", on_console)
        page.on("response", on_response)
        page.on("requestfailed", on_failed)

        control_enabled = False
        wait_timeout_ms = int(float(os.getenv("BRIDGE_WEB_WAIT_TIMEOUT_SECONDS", "12")) * 1000)
        wait_timeout_ms = max(1000, min(60000, wait_timeout_ms))
        interactive_timeout_ms = int(float(os.getenv("BRIDGE_WEB_INTERACTIVE_TIMEOUT_SECONDS", "8")) * 1000)
        interactive_timeout_ms = max(1000, min(60000, interactive_timeout_ms))
        post_action_pause_ms = int(float(os.getenv("BRIDGE_WEB_POST_ACTION_PAUSE_MS", "250")))
        post_action_pause_ms = max(0, min(2000, post_action_pause_ms))
        watchdog_cfg = WebWatchdogConfig(
            stuck_interactive_seconds=float(os.getenv("BRIDGE_WEB_STUCK_INTERACTIVE_SECONDS", "12")),
            stuck_step_seconds=float(os.getenv("BRIDGE_WEB_STUCK_STEP_SECONDS", "20")),
            stuck_iframe_seconds=float(os.getenv("BRIDGE_WEB_STUCK_IFRAME_SECONDS", "8")),
        )
        try:
            page.set_default_timeout(
                max(
                    1000,
                    min(
                        int(timeout_seconds * 1000),
                        int(run_hard_timeout_seconds * 1000),
                    ),
                )
            )
        except Exception:
            pass
        watchdog_state.last_useful_events = _observer_useful_event_count(session)
        watchdog_state.last_step_change_ts = time.monotonic()
        watchdog_state.last_progress_event_ts = watchdog_state.last_step_change_ts
        try:
            learning_context = _learning_context(url, "")
            initial_url = page.url
            initial_title = _safe_page_title(page)
            learning_context = _learning_context(url, initial_title)
            observations.append(f"Initial url/title: {initial_url} | {initial_title}")
            target_matches = _same_origin_path(initial_url, url)
            if target_matches:
                observations.append("Navigation skipped (already at target)")
            else:
                actions.append(f"cmd: playwright goto {url}")
                page.goto(url, wait_until="domcontentloaded")
                observations.append(f"Opened URL: {url}")
                if visual:
                    _ensure_visual_overlay_ready_best_effort(
                        page,
                        ui_findings,
                        cursor_expected=visual_cursor,
                        retries=3,
                        delay_ms=140,
                        debug_screenshot_path=overlay_debug_path,
                        force_reinit=True,
                    )

            if visual:
                _ensure_visual_overlay_ready_best_effort(
                    page,
                    ui_findings,
                    cursor_expected=visual_cursor,
                    retries=3,
                    delay_ms=140,
                    debug_screenshot_path=overlay_debug_path,
                    force_reinit=True,
                )
                _set_assistant_control_overlay(page, True)
                control_enabled = True
                _update_top_bar_state(
                    page,
                    _session_state_payload(session, override_controlled=True),
                )
            observations.append(f"Page title: {_safe_page_title(page)}")
            if attached and session is not None:
                mark_controlled(session, True, url=page.url, title=_safe_page_title(page))

            # Preflight UI context evidence (before executing steps).
            try:
                context_path = evidence_dir / "step_0_context.png"
                page.screenshot(path=str(context_path), full_page=True)
                evidence_paths.append(_to_repo_rel(context_path))
            except Exception:
                pass
            try:
                body_text = page.evaluate(
                    "() => (document.body && document.body.innerText ? document.body.innerText.slice(0, 500) : '')"
                )
            except Exception:
                body_text = ""
            body_snippet = _collapse_ws(str(body_text or ""))[:500]
            ui_findings.append(
                f"context title={_safe_page_title(page)} url={page.url} body[:500]={body_snippet}"
            )

            def _remaining_ms(deadline_ts: float) -> int:
                return watchdog_remaining_ms(deadline_ts, now_ts=time.monotonic())

            def _watchdog_stuck_attempt(attempted: str) -> bool:
                nonlocal handoff_reason, handoff_where, handoff_attempted
                nonlocal force_keep_open, wait_for_human_learning, failed_target_for_teaching
                nonlocal control_enabled, result, release_for_handoff
                now = time.monotonic()
                useful = _observer_useful_event_count(session)
                watchdog_poll_progress(watchdog_state, useful_event_count=useful, now_ts=now)
                stuck_reason = evaluate_stuck_reason(
                    watchdog_state,
                    cfg=watchdog_cfg,
                    now_ts=now,
                    iframe_focus_locked=_is_iframe_focus_locked(page),
                )
                if stuck_reason == "stuck_iframe_focus":
                    handoff_reason = "stuck_iframe_focus"
                    handoff_where = watchdog_state.current_step_signature
                    handoff_attempted = (
                        f"{attempted}, iframe_focus>{watchdog_cfg.stuck_iframe_seconds}s"
                    )
                    failed_target_for_teaching = watchdog_state.current_learning_target
                    force_keep_open = True
                    wait_for_human_learning = False
                    release_for_handoff = True
                    _show_custom_handoff_notice(
                        page, "Me he quedado dentro de YouTube iframe. Te cedo el control."
                    )
                    ui_findings.append("Me he quedado dentro de YouTube iframe. Te cedo el control.")
                    ui_findings.append("what_failed=stuck_iframe_focus")
                    ui_findings.append(f"where={handoff_where}")
                    ui_findings.append(
                        "why_likely=focus/cursor remained in iframe without useful progress"
                    )
                    ui_findings.append(f"attempted={handoff_attempted}")
                    ui_findings.append("next_best_action=human_assist")
                    result = "partial"
                    return True
                if stuck_reason == "stuck":
                    handoff_reason = "stuck"
                    handoff_where = watchdog_state.current_step_signature
                    handoff_attempted = attempted
                    failed_target_for_teaching = watchdog_state.current_learning_target
                    force_keep_open = True
                    wait_for_human_learning = True
                    release_for_handoff = False
                    control_enabled = _trigger_stuck_handoff(
                        page=page,
                        session=session,
                        visual=visual,
                        control_enabled=control_enabled,
                        where=handoff_where,
                        attempted=attempted,
                        learning_window_seconds=int(
                            float(os.getenv("BRIDGE_LEARNING_WINDOW_SECONDS", "25"))
                        ),
                        actions=actions,
                        ui_findings=ui_findings,
                    )
                    result = "partial"
                    return True
                return False

            def _trigger_timeout_handoff(
                *,
                what_failed: str,
                where: str,
                learning_target: str,
                attempted: str,
                why_likely: str,
                notice_message: str,
            ) -> bool:
                nonlocal handoff_reason, handoff_where, handoff_attempted
                nonlocal force_keep_open, wait_for_human_learning, failed_target_for_teaching
                nonlocal control_enabled, result, release_for_handoff
                handoff_reason = what_failed
                handoff_where = where
                handoff_attempted = attempted
                failed_target_for_teaching = (
                    learning_target if _is_learning_target_candidate(learning_target) else ""
                )
                force_keep_open = True
                wait_for_human_learning = True
                release_for_handoff = False
                if teaching_mode:
                    control_enabled = _trigger_stuck_handoff(
                        page=page,
                        session=session,
                        visual=visual,
                        control_enabled=control_enabled,
                        where=where,
                        attempted=attempted,
                        learning_window_seconds=int(float(os.getenv("BRIDGE_LEARNING_WINDOW_SECONDS", "25"))),
                        actions=actions,
                        ui_findings=ui_findings,
                        what_failed=what_failed,
                        notice_message=notice_message,
                        why_likely=why_likely,
                    )
                    result = "partial"
                    return True
                ui_findings.append(f"what_failed={what_failed}")
                ui_findings.append(f"where={where}")
                ui_findings.append(f"why_likely={why_likely}")
                ui_findings.append(f"attempted={attempted}")
                ui_findings.append("next_best_action=inspect logs and retry")
                result = "failed"
                return True

            interactive_step = 0
            total = len(steps)
            step_outcomes: list[dict[str, Any]] = []

            def _record_step_outcome(*, idx: int, step: WebStep, status: str, reason: str = "") -> None:
                payload = {
                    "index": int(idx),
                    "kind": str(step.kind),
                    "target": str(step.target),
                    "status": str(status),
                }
                if reason:
                    payload["reason"] = str(reason)
                step_outcomes.append(payload)
                ui_findings.append(f"step_status={json.dumps(payload, ensure_ascii=False)}")

            for idx, step in enumerate(steps, start=1):
                attempted_hint = ""
                step_sig = f"step {idx}/{total} {step.kind}:{step.target}"
                step_learning_target = _step_learning_target(step.kind, step.target)
                update_step_signature(
                    watchdog_state,
                    step_signature=step_sig,
                    learning_target=step_learning_target,
                    now_ts=time.monotonic(),
                )
                if _runtime_closed(page, session):
                    result = "failed"
                    append_run_crash_findings(ui_findings)
                    break
                if time.monotonic() > run_deadline_ts:
                    if _trigger_timeout_handoff(
                        what_failed="run_timeout",
                        where=watchdog_state.current_step_signature or "web-run",
                        learning_target="",
                        attempted="run hard timeout exceeded",
                        why_likely=(
                            "run exceeded BRIDGE_WEB_RUN_HARD_TIMEOUT_SECONDS without completing all steps"
                        ),
                        notice_message="He excedido el tiempo máximo del run. Te cedo el control.",
                    ):
                        break
                try:
                    step_budget_ms = max(
                        800,
                        min(
                            int(step_hard_timeout_seconds * 1000),
                            _remaining_ms(run_deadline_ts),
                        ),
                    )
                    page.set_default_timeout(step_budget_ms)
                except Exception:
                    pass
                if teaching_mode and _watchdog_stuck_attempt("watchdog:loop"):
                    break
                if progress_cb:
                    progress_cb(idx, total, f"web step {idx}/{total}: {step.kind}")
                if visual:
                    _ensure_visual_overlay_ready_best_effort(
                        page,
                        ui_findings,
                        cursor_expected=visual_cursor,
                        retries=3,
                        delay_ms=120,
                        debug_screenshot_path=overlay_debug_path,
                        force_reinit=True,
                    )

                if step.kind in INTERACTIVE_STEP_KINDS:
                    skip_reason = _interactive_step_not_applicable_reason(page, step)
                    if skip_reason:
                        observations.append(
                            f"Step {idx}: skipped_not_applicable {step.kind}:{step.target} ({skip_reason})"
                        )
                        ui_findings.append(
                            f"step {idx} skipped_not_applicable: {step.kind}:{step.target} reason={skip_reason}"
                        )
                        _record_step_outcome(
                            idx=idx,
                            step=step,
                            status="skipped_not_applicable",
                            reason=skip_reason,
                        )
                        watchdog_state.last_progress_event_ts = time.monotonic()
                        continue
                    step_started_at = time.monotonic()
                    step_deadline_ts = step_started_at + step_hard_timeout_seconds
                    if min(_remaining_ms(step_deadline_ts), _remaining_ms(run_deadline_ts)) <= 0:
                        if _trigger_timeout_handoff(
                            what_failed="interactive_timeout",
                            where=watchdog_state.current_step_signature or f"step {idx}/{total}",
                            learning_target=step.target,
                            attempted="step hard timeout precheck",
                            why_likely=(
                                "interactive step exceeded BRIDGE_WEB_STEP_HARD_TIMEOUT_SECONDS before execution"
                            ),
                            notice_message="El paso interactivo superó el tiempo límite. Te cedo el control.",
                        ):
                            break
                    if not _force_main_frame_context(page):
                        if teaching_mode:
                            force_keep_open = True
                            wait_for_human_learning = False
                            handoff_reason = "stuck_iframe_focus"
                            handoff_where = watchdog_state.current_step_signature
                            handoff_attempted = "main-frame-first precheck failed"
                            failed_target_for_teaching = step.target
                            release_for_handoff = True
                            _show_custom_handoff_notice(
                                page, "Me he quedado dentro de YouTube iframe. Te cedo el control."
                            )
                            append_iframe_focus_findings(
                                ui_findings,
                                where=handoff_where,
                                attempted=handoff_attempted,
                                why_likely=(
                                    "unable to return focus/context to main frame before interactive action"
                                ),
                            )
                            result = "partial"
                            break
                        raise RuntimeError("Unable to return to main frame context before interactive step")
                    interactive_step += 1
                    before = evidence_dir / f"step_{interactive_step}_before.png"
                    after = evidence_dir / f"step_{interactive_step}_after.png"
                    page.screenshot(path=str(before), full_page=False)
                    evidence_paths.append(_to_repo_rel(before))
                    prev_action_len = len(actions)
                    attempted_hint = ""
                    try:
                        effective_timeout_ms = min(
                            interactive_timeout_ms,
                            max(250, _remaining_ms(step_deadline_ts)),
                            max(250, _remaining_ms(run_deadline_ts)),
                        )
                        if effective_timeout_ms <= 250 and (
                            _remaining_ms(step_deadline_ts) <= 0 or _remaining_ms(run_deadline_ts) <= 0
                        ):
                            if _trigger_timeout_handoff(
                                what_failed="interactive_timeout",
                                where=watchdog_state.current_step_signature or f"step {idx}/{total}",
                                learning_target=step.target,
                                attempted="step hard timeout in interactive execution",
                                why_likely=(
                                    "interactive action budget exhausted before action started"
                                ),
                                notice_message="El paso interactivo superó el tiempo límite. Te cedo el control.",
                            ):
                                break
                        if teaching_mode:
                            retry_result = _apply_interactive_step_with_retries(
                                page,
                                step,
                                interactive_step,
                                evidence_dir,
                                actions,
                                observations,
                                ui_findings,
                                evidence_paths,
                                visual=visual,
                                click_pulse_enabled=visual_click_pulse,
                                visual_human_mouse=visual_human_mouse,
                                visual_mouse_speed=visual_mouse_speed,
                                visual_click_hold_ms=visual_click_hold_ms,
                                timeout_ms=effective_timeout_ms,
                                max_retries=2,
                                learning_selectors=_learned_selectors_for_step(
                                    step, learned_selector_map, learning_context
                                ),
                                session=session,
                                step_label=f"web step {idx}/{total}: {step.kind}:{step.target}",
                                stuck_interactive_seconds=float(
                                    os.getenv("BRIDGE_WEB_STUCK_INTERACTIVE_SECONDS", "12")
                                ),
                                stuck_step_seconds=float(os.getenv("BRIDGE_WEB_STUCK_STEP_SECONDS", "20")),
                                step_deadline_ts=step_deadline_ts,
                                run_deadline_ts=run_deadline_ts,
                            )
                            if bool(getattr(retry_result, "deadline_hit", False)):
                                if _trigger_timeout_handoff(
                                    what_failed="interactive_timeout",
                                    where=watchdog_state.current_step_signature or f"step {idx}/{total}",
                                    learning_target=step.target,
                                    attempted=retry_result.attempted or "step hard timeout",
                                    why_likely=(
                                        "interactive retries exceeded hard timeout without completing action"
                                    ),
                                    notice_message="El paso interactivo superó el tiempo límite. Te cedo el control.",
                                ):
                                    break
                            if retry_result.stuck:
                                force_keep_open = True
                                wait_for_human_learning = True
                                handoff_reason = "stuck"
                                handoff_where = watchdog_state.current_step_signature
                                handoff_attempted = retry_result.attempted
                                failed_target_for_teaching = step.target
                                release_for_handoff = False
                                control_enabled = _trigger_stuck_handoff(
                                    page=page,
                                    session=session,
                                    visual=visual,
                                    control_enabled=control_enabled,
                                    where=handoff_where,
                                    attempted=handoff_attempted,
                                    learning_window_seconds=int(
                                        float(os.getenv("BRIDGE_LEARNING_WINDOW_SECONDS", "25"))
                                    ),
                                    actions=actions,
                                    ui_findings=ui_findings,
                                )
                                result = "partial"
                                break
                            attempted_hint = retry_result.attempted
                            if retry_result.selector_used:
                                learning_notes.append(
                                    f"selector used for target '{step.target}': {retry_result.selector_used}"
                                )
                                _store_learned_selector(
                                    target=step.target,
                                    selector=retry_result.selector_used,
                                    context=learning_context,
                                    source="auto_retry",
                                )
                        else:
                            _apply_interactive_step(
                                page,
                                step,
                                interactive_step,
                                actions,
                                observations,
                                ui_findings,
                                visual=visual,
                                click_pulse_enabled=visual_click_pulse,
                                visual_human_mouse=visual_human_mouse,
                                visual_mouse_speed=visual_mouse_speed,
                                visual_click_hold_ms=visual_click_hold_ms,
                                timeout_ms=effective_timeout_ms,
                                movement_capture_dir=evidence_dir,
                                evidence_paths=evidence_paths,
                            )
                    except Exception as exc:
                        if _is_page_closed_error(exc) or _runtime_closed(page, session):
                            result = "failed"
                            append_run_crash_findings(ui_findings)
                            break
                        if teaching_mode and step.kind in TEACHING_HANDOFF_KINDS:
                            force_keep_open = True
                            release_for_handoff = True
                            wait_for_human_learning = True
                            failed_target_for_teaching = step.target
                            learning_notes.append(f"failed target: {step.target}")
                            ui_findings.append(
                                f"No encuentro el botón: {step.target}. Te cedo el control."
                            )
                            ui_findings.append("what_failed=target_not_found")
                            ui_findings.append(
                                f"where=step {interactive_step}:{step.kind}:{step.target}"
                            )
                            ui_findings.append(
                                "why_likely=target text/selector changed, hidden, or not yet rendered"
                            )
                            ui_findings.append(
                                "attempted=stable selector candidates + container/page scroll retries"
                            )
                            ui_findings.append("next_best_action=human_assist")
                            _show_teaching_handoff_notice(page, step.target)
                            result = "partial"
                            break
                        if _is_timeout_error(exc):
                            timeout_path = evidence_dir / f"step_{interactive_step}_timeout.png"
                            try:
                                page.screenshot(path=str(timeout_path), full_page=False)
                                evidence_paths.append(_to_repo_rel(timeout_path))
                            except Exception:
                                pass
                            console_errors.append(
                                f"Timeout on interactive step {interactive_step}: {step.kind} {step.target}"
                            )
                            append_interactive_timeout_findings(
                                ui_findings,
                                step_num=interactive_step,
                                step_kind=step.kind,
                                step_target=step.target,
                                timeout_ms=interactive_timeout_ms,
                            )
                            result = "failed"
                            break
                        raise
                    if handoff_reason in {"stuck", "stuck_iframe_focus"}:
                        break
                    if len(actions) > prev_action_len:
                        watchdog_state.last_progress_event_ts = time.monotonic()
                    _record_step_outcome(idx=idx, step=step, status="executed")
                    if post_action_pause_ms > 0:
                        page.wait_for_timeout(post_action_pause_ms)
                    page.screenshot(path=str(after), full_page=False)
                    evidence_paths.append(_to_repo_rel(after))
                    if visual:
                        _ensure_visual_overlay_ready_best_effort(
                            page,
                            ui_findings,
                            cursor_expected=visual_cursor,
                            retries=3,
                            delay_ms=120,
                            debug_screenshot_path=overlay_debug_path,
                            force_reinit=True,
                        )
                    continue

                try:
                    step_started_at = time.monotonic()
                    step_deadline_ts = step_started_at + step_hard_timeout_seconds
                    effective_wait_timeout_ms = min(
                        wait_timeout_ms,
                        max(250, _remaining_ms(step_deadline_ts)),
                        max(250, _remaining_ms(run_deadline_ts)),
                    )
                    if effective_wait_timeout_ms <= 250 and (
                        _remaining_ms(step_deadline_ts) <= 0 or _remaining_ms(run_deadline_ts) <= 0
                    ):
                        if _trigger_timeout_handoff(
                            what_failed="interactive_timeout",
                            where=watchdog_state.current_step_signature or f"step {idx}/{total}",
                            learning_target=step.target,
                            attempted="step hard timeout before wait",
                            why_likely="wait step deadline exceeded before operation",
                            notice_message="El paso superó el tiempo límite. Te cedo el control.",
                        ):
                            break
                    if not _force_main_frame_context(page):
                        if teaching_mode:
                            force_keep_open = True
                            wait_for_human_learning = False
                            handoff_reason = "stuck_iframe_focus"
                            handoff_where = watchdog_state.current_step_signature
                            handoff_attempted = "main-frame-first precheck failed"
                            failed_target_for_teaching = step.target
                            release_for_handoff = True
                            _show_custom_handoff_notice(
                                page, "Me he quedado dentro de YouTube iframe. Te cedo el control."
                            )
                            append_iframe_focus_findings(
                                ui_findings,
                                where=handoff_where,
                                attempted=handoff_attempted,
                                why_likely=(
                                    "unable to return focus/context to main frame before wait step"
                                ),
                            )
                            result = "partial"
                            break
                        raise RuntimeError("Unable to return to main frame context before wait step")
                    _apply_wait_step(
                        page,
                        step,
                        idx,
                        actions,
                        observations,
                        ui_findings,
                        timeout_ms=effective_wait_timeout_ms,
                    )
                except Exception as exc:
                    if _is_page_closed_error(exc) or _runtime_closed(page, session):
                        result = "failed"
                        append_run_crash_findings(ui_findings)
                        break
                    if _is_timeout_error(exc):
                        if _should_soft_skip_wait_timeout(steps=steps, idx=idx, step=step, teaching_mode=teaching_mode):
                            observations.append(
                                "teaching wait soft-skip: timed out on wait_text but next step is Stop"
                            )
                            ui_findings.append(
                                f"step {idx} soft-skip wait timeout on {step.kind}:{step.target} (teaching)"
                            )
                            _record_step_outcome(
                                idx=idx,
                                step=step,
                                status="skipped_not_applicable",
                                reason="teaching soft-skip wait timeout",
                            )
                            continue
                        timeout_path = evidence_dir / f"step_{idx}_timeout.png"
                        try:
                            page.screenshot(path=str(timeout_path), full_page=False)
                            evidence_paths.append(_to_repo_rel(timeout_path))
                        except Exception:
                            pass
                        console_errors.append(f"Timeout on step {idx}: {step.kind} {step.target}")
                        append_wait_timeout_findings(
                            ui_findings,
                            step_num=idx,
                            step_kind=step.kind,
                            step_target=step.target,
                            timeout_ms=wait_timeout_ms,
                        )
                        result = "failed"
                        break
                    raise
                if visual:
                    _ensure_visual_overlay_ready_best_effort(
                        page,
                        ui_findings,
                        cursor_expected=visual_cursor,
                        retries=3,
                        delay_ms=120,
                        debug_screenshot_path=overlay_debug_path,
                        force_reinit=True,
                    )
                watchdog_state.last_progress_event_ts = time.monotonic()
                _record_step_outcome(idx=idx, step=step, status="executed")
                if teaching_mode and _watchdog_stuck_attempt(
                    attempted_hint or f"watchdog:post-step:{step.kind}"
                ):
                    break
            ui_findings.append(f"steps_outcome={json.dumps(step_outcomes, ensure_ascii=False)}")
            if release_for_handoff and handoff_reason != "stuck" and session is not None:
                mark_controlled(session, False, url=page.url, title=_safe_page_title(page))
                if wait_for_human_learning:
                    _notify_learning_state(
                        session,
                        active=True,
                        window_seconds=int(float(os.getenv("BRIDGE_LEARNING_WINDOW_SECONDS", "25"))),
                    )
                if visual and control_enabled:
                    _set_assistant_control_overlay(page, False)
                    if wait_for_human_learning:
                        _set_learning_handoff_overlay(page, True)
                    else:
                        _set_user_control_overlay(page, True)
                    _update_top_bar_state(
                        page,
                        _session_state_payload(
                            session,
                            override_controlled=False,
                            learning_active=bool(wait_for_human_learning),
                        ),
                    )
                    control_enabled = False
                actions.append("cmd: playwright release control (teaching handoff)")
                if "control released" not in ui_findings:
                    ui_findings.append("control released")
            if wait_for_human_learning:
                learn = None
                if session is not None:
                    _notify_learning_state(
                        session,
                        active=True,
                        window_seconds=int(float(os.getenv("BRIDGE_LEARNING_WINDOW_SECONDS", "25"))),
                    )
                    try:
                        _update_top_bar_state(
                            page,
                            _session_state_payload(session, override_controlled=False, learning_active=True),
                        )
                    except Exception:
                        pass
                learning_iframe_guard = _disable_active_youtube_iframe_pointer_events(page)
                if session is not None:
                    learn = _capture_manual_learning(
                        page=page,
                        session=session,
                        failed_target=failed_target_for_teaching,
                        context=learning_context,
                        wait_seconds=int(float(os.getenv("BRIDGE_LEARNING_WINDOW_SECONDS", "25"))),
                    )
                if learn:
                    selector_used = str(learn.get("selector", "")).strip()
                    if not selector_used:
                        target_hint = str(learn.get("target", "")).strip()
                        stable = _stable_selectors_for_target(target_hint)
                        selector_used = stable[0] if stable else ""
                    if selector_used:
                        _store_learned_selector(
                            target=str(learn.get("failed_target", "")).strip(),
                            selector=selector_used,
                            context=learning_context,
                            source="manual",
                        )
                    artifact_paths = _write_teaching_artifacts(run_dir, learn)
                    evidence_paths.extend(artifact_paths)
                    _show_learning_thanks_notice(page, str(learn.get("failed_target", "")).strip())
                    observations.append(
                        "Teaching mode learned selector from manual action: "
                        f"{selector_used or learn.get('target', '')}"
                    )
                    ui_findings.append(
                        "Gracias, ya he aprendido dónde está el botón "
                        f"{str(learn.get('failed_target', '')).strip()}. Ya continúo yo."
                    )
                    resumed = _resume_after_learning(
                        page=page,
                        selector=selector_used,
                        target=str(learn.get("failed_target", "")).strip(),
                        actions=actions,
                        observations=observations,
                        ui_findings=ui_findings,
                    )
                    if resumed:
                        observations.append("teaching resume: action replayed after learning")
                    else:
                        ui_findings.append("learning_resume=failed")
                else:
                    ui_findings.append("learning_capture=none")
                _set_learning_handoff_overlay(page, False)
                _restore_iframe_pointer_events(page, learning_iframe_guard)
                learning_iframe_guard = None
                if session is not None:
                    _notify_learning_state(session, active=False, window_seconds=1)
                    try:
                        _update_top_bar_state(
                            page,
                            _session_state_payload(session, override_controlled=False, learning_active=False),
                        )
                    except Exception:
                        pass
        finally:
            try:
                _restore_iframe_pointer_events(page, learning_iframe_guard)
            except Exception:
                pass
            try:
                _set_learning_handoff_overlay(page, False)
            except Exception:
                pass
            if visual and control_enabled:
                _set_assistant_control_overlay(page, False)
                if session is not None:
                    _update_top_bar_state(
                        page,
                        _session_state_payload(session, override_controlled=False, learning_active=False),
                    )
                ui_findings.append("control released")
            if attached and session is not None:
                mark_controlled(session, False, url=page.url, title=_safe_page_title(page))
            if not attached and not keep_open and not force_keep_open:
                browser.close()

    result = locals().get("result", "success")
    result = _finalize_result(
        result=result,
        force_keep_open=force_keep_open,
        console_errors=console_errors,
        network_findings=network_findings,
        verified=verified,
        steps_count=len(steps),
        ui_findings=ui_findings,
        where_default=watchdog_state.current_step_signature or "web-run",
    )

    return OIReport(
        task_id=run_dir.name,
        goal=f"web: {url}",
        actions=actions,
        observations=observations,
        console_errors=console_errors,
        network_findings=network_findings,
        ui_findings=ui_findings,
        result=result,
        evidence_paths=evidence_paths,
    )


@dataclass(frozen=True)
class _RetryResult:
    selector_used: str = ""
    stuck: bool = False
    attempted: str = ""
    deadline_hit: bool = False


def _apply_interactive_step_with_retries(
    page: Any,
    step: WebStep,
    step_num: int,
    evidence_dir: Path,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    evidence_paths: list[str],
    *,
    visual: bool,
    click_pulse_enabled: bool,
    visual_human_mouse: bool,
    visual_mouse_speed: float,
    visual_click_hold_ms: int,
    timeout_ms: int,
    max_retries: int,
    learning_selectors: list[str],
    session: WebSession | None,
    step_label: str,
    stuck_interactive_seconds: float,
    stuck_step_seconds: float,
    step_deadline_ts: float,
    run_deadline_ts: float,
) -> _RetryResult:
    candidates: list[WebStep] = [step]
    if step.kind == "click_text":
        if not _is_generic_play_label(step.target):
            for selector in _stable_selectors_for_target(step.target):
                candidates.append(WebStep("click_selector", selector))
            for selector in learning_selectors:
                candidates.insert(1, WebStep("click_selector", selector))
    elif step.kind == "click_selector":
        for selector in learning_selectors:
            candidates.insert(0, WebStep("click_selector", selector))
        if not _is_specific_selector(step.target):
            for hint in _semantic_hints_for_selector(step.target):
                candidates.append(WebStep("click_text", hint))
                for selector in _stable_selectors_for_target(hint):
                    candidates.append(WebStep("click_selector", selector))

    last_exc: Exception | None = None
    total_attempts = max(1, int(max_retries) + 1)
    started_at = time.monotonic()
    baseline_events = _observer_useful_event_count(session)
    attempted_parts: list[str] = []
    for attempt in range(1, total_attempts + 1):
        now = time.monotonic()
        if now > step_deadline_ts or now > run_deadline_ts:
            attempted = ", ".join((attempted_parts + ["deadline=step_or_run"])[:18])
            return _RetryResult(selector_used="", stuck=False, attempted=attempted, deadline_hit=True)
        attempted_parts.append(f"retry={attempt - 1}")
        if attempt > 1:
            _retry_scroll(page)
            attempted_parts.append("scroll=main+page")
            ui_findings.append(f"step {step_num} retry {attempt - 1}/{max_retries}: scrolled and re-attempting")
        before_retry = evidence_dir / f"step_{step_num}_retry_{attempt}_before.png"
        after_retry = evidence_dir / f"step_{step_num}_retry_{attempt}_after.png"
        try:
            page.screenshot(path=str(before_retry), full_page=False)
            evidence_paths.append(_to_repo_rel(before_retry))
        except Exception:
            pass
        for candidate in candidates:
            now = time.monotonic()
            if now > step_deadline_ts or now > run_deadline_ts:
                attempted = ", ".join((attempted_parts + ["deadline=step_or_run"])[:18])
                return _RetryResult(selector_used="", stuck=False, attempted=attempted, deadline_hit=True)
            try:
                if candidate.kind == "click_selector":
                    attempted_parts.append(f"selector={candidate.target}")
                _apply_interactive_step(
                    page,
                    candidate,
                    step_num,
                    actions,
                    observations,
                    ui_findings,
                    visual=visual,
                    click_pulse_enabled=click_pulse_enabled,
                    visual_human_mouse=visual_human_mouse,
                    visual_mouse_speed=visual_mouse_speed,
                    visual_click_hold_ms=visual_click_hold_ms,
                    timeout_ms=timeout_ms,
                    movement_capture_dir=evidence_dir,
                    evidence_paths=evidence_paths,
                )
                try:
                    page.screenshot(path=str(after_retry), full_page=False)
                    evidence_paths.append(_to_repo_rel(after_retry))
                except Exception:
                    pass
                if candidate.kind == "click_selector" and candidate.target != step.target:
                    observations.append(
                        f"step {step_num} used stable selector fallback: {candidate.target}"
                    )
                    return _RetryResult(selector_used=candidate.target)
                return _RetryResult(selector_used="")
            except Exception as exc:
                last_exc = exc
                if _should_mark_stuck(
                    started_at=started_at,
                    session=session,
                    baseline_useful_events=baseline_events,
                    stuck_interactive_seconds=stuck_interactive_seconds,
                    stuck_step_seconds=stuck_step_seconds,
                ):
                    attempted = ", ".join(attempted_parts[-18:])
                    ui_findings.append(
                        f"stuck detected on {step_label}: elapsed>{stuck_interactive_seconds}s "
                        "and no useful observer events"
                    )
                    return _RetryResult(selector_used="", stuck=True, attempted=attempted)
                continue
        try:
            page.screenshot(path=str(after_retry), full_page=False)
            evidence_paths.append(_to_repo_rel(after_retry))
        except Exception:
            pass
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Failed interactive step after retries: {step.kind} {step.target}")


def _should_mark_stuck(
    *,
    started_at: float,
    session: WebSession | None,
    baseline_useful_events: int,
    stuck_interactive_seconds: float,
    stuck_step_seconds: float,
) -> bool:
    elapsed = max(0.0, time.monotonic() - started_at)
    no_useful_events = True
    current_useful = _observer_useful_event_count(session)
    if current_useful > baseline_useful_events:
        no_useful_events = False

    if elapsed > max(0.1, float(stuck_step_seconds)):
        return True
    if elapsed > max(0.1, float(stuck_interactive_seconds)) and no_useful_events:
        return True
    return False


def _observer_useful_event_count(session: WebSession | None) -> int:
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


def _learning_context(url: str, title: str) -> dict[str, str]:
    parsed = urlparse(str(url))
    hostname = str(parsed.netloc or "").lower()
    path = str(parsed.path or "/")
    title_norm = _collapse_ws(title).lower()[:80]
    return {
        "hostname": hostname,
        "path": path,
        "title_hint": title_norm,
        "state_key": f"{hostname}{path}|{title_norm}",
    }


def _load_learned_selectors() -> dict[str, dict[str, list[str]]]:
    try:
        if not _LEARNING_JSON.exists():
            return {}
        payload = json.loads(_LEARNING_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        entry: dict[str, list[str]] = {}
        for tgt, selectors in value.items():
            if isinstance(tgt, str) and isinstance(selectors, list):
                entry[tgt] = [str(s).strip() for s in selectors if str(s).strip()]
        if entry:
            out[str(key)] = entry
    return out


def _store_learned_selector(
    *,
    target: str,
    selector: str,
    context: dict[str, str],
    source: str,
) -> None:
    target_norm = _normalize_learning_target_key(target, selector=selector)
    selector_norm = str(selector).strip()
    if not target_norm or not selector_norm:
        return
    if not _is_specific_selector(selector_norm):
        return
    all_map = _load_learned_selectors()
    state_key = str(context.get("state_key", "")).strip()
    if not state_key:
        return
    state_bucket = all_map.setdefault(state_key, {})
    selectors = state_bucket.setdefault(target_norm, [])
    if selector_norm in selectors:
        return
    selectors.insert(0, selector_norm)
    state_bucket[target_norm] = selectors[:6]
    _LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    _LEARNING_JSON.write_text(json.dumps(all_map, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_learning_audit(target_norm, selector_norm, context, source)


def _write_learning_audit(target: str, selector: str, context: dict[str, str], source: str) -> None:
    audit = _LEARNING_DIR / "web_teaching_audit.md"
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        f"- {now} target=`{target}` selector=`{selector}` source=`{source}`",
        f"  - context: {context.get('state_key', '')}",
    ]
    with audit.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _normalize_learning_target_key(raw: str, *, selector: str = "") -> str:
    text = str(raw or "").strip().lower()
    sel = str(selector or "").strip().lower()
    probe = _normalize_failed_target_label(text).lower() or text
    merged = " ".join([text, probe, sel]).strip()
    if not merged:
        return ""
    # Avoid persisting noisy step signatures as learning keys.
    if text.startswith("step ") and ("click_" in text or "wait_" in text):
        return ""
    cleaned = re.sub(r"[^a-z0-9]+", " ", probe).strip()
    if not cleaned:
        return ""
    return cleaned[:48]


def _is_learning_target_candidate(target: str) -> bool:
    text = str(target or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith("step ") and ("wait_" in lowered or "click_" in lowered):
        return False
    return True


def _is_specific_selector(selector: str) -> bool:
    low = str(selector or "").strip().lower()
    if not low:
        return False
    if ":has-text(" in low:
        return False
    return "__bridge_" not in low


def _learned_selectors_for_step(
    step: WebStep,
    selector_map: dict[str, dict[str, list[str]]],
    context: dict[str, str],
) -> list[str]:
    if step.kind not in {"click_text", "click_selector"}:
        return []
    state_key = str(context.get("state_key", "")).strip()
    if not state_key:
        return []
    bucket = selector_map.get(state_key, {})
    raw_key = str(step.target).strip().lower()
    norm_key = _normalize_learning_target_key(step.target)
    out: list[str] = []
    for key in (norm_key, raw_key):
        if not key:
            continue
        for selector in bucket.get(key, []):
            if not _is_specific_selector(selector):
                continue
            if step.kind == "click_selector" and str(step.target).strip() and selector != str(step.target).strip():
                continue
            if selector not in out:
                out.append(selector)
    return out


def _should_soft_skip_wait_timeout(
    *, steps: list[WebStep], idx: int, step: WebStep, teaching_mode: bool
) -> bool:
    if not teaching_mode or step.kind != "wait_text":
        return False
    remaining = steps[idx:]
    return any(candidate.kind in INTERACTIVE_STEP_KINDS for candidate in remaining)


def _prioritize_steps_with_learned_selectors(
    steps: list[WebStep],
    selector_map: dict[str, dict[str, list[str]]],
    context: dict[str, str],
) -> list[WebStep]:
    if not steps:
        return steps
    out: list[WebStep] = []
    for step in steps:
        out.append(step)
        learned = _learned_selectors_for_step(step, selector_map, context)
        if step.kind == "click_text" and learned:
            out.pop()
            for selector in learned:
                out.append(WebStep("click_selector", selector))
            out.append(step)
    return out


def _show_teaching_handoff_notice(page: Any, target: str) -> None:
    _teaching_show_handoff_notice(page, target)


def _show_stuck_handoff_notice(page: Any, step_text: str) -> None:
    _handoff_show_stuck_notice(page, step_text)


def _show_custom_handoff_notice(page: Any, message: str) -> None:
    _handoff_show_custom_notice(page, message)


def _trigger_stuck_handoff(
    *,
    page: Any,
    session: WebSession | None,
    visual: bool,
    control_enabled: bool,
    where: str,
    attempted: str,
    learning_window_seconds: int,
    actions: list[str],
    ui_findings: list[str],
    what_failed: str = "stuck",
    notice_message: str = "",
    why_likely: str = "step unchanged/no useful progress within stuck thresholds during teaching mode",
) -> bool:
    return _handoff_trigger_stuck(
        page=page,
        session=session,
        visual=visual,
        control_enabled=control_enabled,
        where=where,
        attempted=attempted,
        learning_window_seconds=learning_window_seconds,
        actions=actions,
        ui_findings=ui_findings,
        what_failed=what_failed,
        notice_message=notice_message,
        why_likely=why_likely,
        show_custom_notice=_show_custom_handoff_notice,
        show_stuck_notice=_show_stuck_handoff_notice,
        set_learning_handoff_overlay=_set_learning_handoff_overlay,
        set_assistant_control_overlay=_set_assistant_control_overlay,
        mark_controlled=mark_controlled,
        safe_page_title=_safe_page_title,
        notify_learning_state=_notify_learning_state,
        update_top_bar_state=_update_top_bar_state,
        session_state_payload=_session_state_payload,
    )


def _is_iframe_focus_locked(page: Any) -> bool:
    return _frame_is_iframe_focus_locked(page)


def _disable_active_youtube_iframe_pointer_events(page: Any) -> dict[str, Any] | None:
    return _frame_disable_active_youtube_iframe_pointer_events(
        page,
        page_is_closed=_page_is_closed,
    )


def _restore_iframe_pointer_events(page: Any, token: dict[str, Any] | None) -> None:
    _frame_restore_iframe_pointer_events(
        page,
        token,
        page_is_closed=_page_is_closed,
    )


def _force_main_frame_context(page: Any, max_seconds: float = 8.0) -> bool:
    return _frame_force_main_frame_context(
        page,
        max_seconds=max_seconds,
        iframe_focus_locked=_is_iframe_focus_locked,
    )


def _show_learning_thanks_notice(page: Any, target: str) -> None:
    _teaching_show_learning_thanks_notice(page, target)


def _normalize_failed_target_label(raw: str) -> str:
    return _teaching_normalize_failed_target_label(raw)


def _show_wrong_manual_click_notice(page: Any, failed_target: str) -> None:
    _teaching_show_wrong_click_notice(page, failed_target, _stable_selectors_for_target)


def _capture_manual_learning(
    *,
    page: Any | None,
    session: WebSession,
    failed_target: str,
    context: dict[str, str],
    wait_seconds: int,
) -> dict[str, Any] | None:
    return _teaching_capture_manual_learning(
        page=page,
        session=session,
        failed_target=failed_target,
        context=context,
        wait_seconds=wait_seconds,
        request_session_state=request_session_state,
        show_wrong_click_notice=_show_wrong_manual_click_notice,
    )


def _is_relevant_manual_learning_event(evt: dict[str, Any], failed_target: str) -> bool:
    return _teaching_is_relevant_manual_learning_event(evt, failed_target)


def _write_teaching_artifacts(run_dir: Path, payload: dict[str, Any]) -> list[str]:
    return _teaching_write_artifacts(run_dir, payload, _to_repo_rel)


def _resume_after_learning(
    *,
    page: Any,
    selector: str,
    target: str,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
) -> bool:
    return _teaching_resume_after_learning(
        page=page,
        selector=selector,
        target=target,
        actions=actions,
        observations=observations,
        ui_findings=ui_findings,
    )


def _scan_visible_buttons_in_cards(
    page: Any,
    *,
    card_selector: str,
    button_selector: str,
    required_text: str,
    seen: set[str],
) -> tuple[list[str], bool]:
    return _bulk_scan_visible_buttons_in_cards(
        page,
        card_selector=card_selector,
        button_selector=button_selector,
        required_text=required_text,
        seen=seen,
    )


def _scan_visible_selectors(page: Any, *, button_selector: str, seen: set[str]) -> list[str]:
    return _bulk_scan_visible_selectors(page, button_selector=button_selector, seen=seen)


def _apply_interactive_step(
    page: Any,
    step: WebStep,
    step_num: int,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    *,
    visual: bool = False,
    click_pulse_enabled: bool = True,
    visual_human_mouse: bool = True,
    visual_mouse_speed: float = 1.0,
    visual_click_hold_ms: int = 180,
    timeout_ms: int = 8000,
    movement_capture_dir: Path | None = None,
    evidence_paths: list[str] | None = None,
) -> None:
    iframe_guard = _disable_active_youtube_iframe_pointer_events(page)
    if not _force_main_frame_context(page):
        _restore_iframe_pointer_events(page, iframe_guard)
        raise RuntimeError("Unable to enforce main frame context for interactive step")
    try:
        move_capture_count = 0

        def _capture_movement(tag: str) -> None:
            nonlocal move_capture_count
            move_capture_count = _capture_movement_artifact(
                page=page,
                tag=tag,
                step_num=step_num,
                move_capture_count=move_capture_count,
                visual=visual,
                movement_capture_dir=movement_capture_dir,
                evidence_paths=evidence_paths,
                get_last_human_route=_get_last_human_route,
                to_repo_rel=_to_repo_rel,
            )

        if step.kind == "click_selector":
            locator = page.locator(step.target).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            target = _highlight_target(
                page,
                locator,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled and visual,
                show_preview=not (visual and visual_human_mouse),
            )
            if target is None:
                raise SystemExit(f"Target occluded or not visible: selector {step.target}")
            if visual:
                if visual_human_mouse and target:
                    _human_mouse_click(
                        page,
                        target[0],
                        target[1],
                        speed=visual_mouse_speed,
                        hold_ms=visual_click_hold_ms,
                    )
                    _capture_movement("after_click_selector")
                else:
                    locator.click(timeout=timeout_ms)
            else:
                locator.click(timeout=timeout_ms)
            actions.append(f"cmd: playwright click selector:{step.target}")
            observations.append(f"Clicked selector in step {step_num}: {step.target}")
            ui_findings.append(
                f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}"
            )
            return

        if step.kind == "click_text":
            locator = page.locator("body").get_by_text(step.target, exact=False).first
            try:
                locator.wait_for(state="visible", timeout=timeout_ms)
                target = _highlight_target(
                    page,
                    locator,
                    f"step {step_num}",
                    click_pulse_enabled=click_pulse_enabled and visual,
                    show_preview=not (visual and visual_human_mouse),
                )
                if target is None:
                    raise SystemExit(f"Target occluded or not visible: text {step.target}")
                if visual:
                    if visual_human_mouse and target:
                        _human_mouse_click(
                            page,
                            target[0],
                            target[1],
                            speed=visual_mouse_speed,
                            hold_ms=visual_click_hold_ms,
                        )
                        _capture_movement("after_click_text")
                    else:
                        locator.click(timeout=timeout_ms)
                else:
                    locator.click(timeout=timeout_ms)
                actions.append(f"cmd: playwright click text:{step.target}")
                observations.append(f"Clicked text in step {step_num}: {step.target}")
                ui_findings.append(
                    f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}"
                )
                return
            except Exception as exc:
                if _is_timeout_error(exc):
                    raise
                raise

        if step.kind == "maybe_click_text":
            locator = page.locator("body").get_by_text(step.target, exact=False).first
            try:
                locator.wait_for(state="visible", timeout=timeout_ms)
                target = _highlight_target(
                    page,
                    locator,
                    f"step {step_num}",
                    click_pulse_enabled=click_pulse_enabled and visual,
                    show_preview=not (visual and visual_human_mouse),
                )
                if target is None:
                    observations.append(f"Step {step_num}: maybe click target not visible/occluded: {step.target}")
                    ui_findings.append(f"step {step_num} verify optional click skipped: {step.target}")
                    return
                if visual and visual_human_mouse and target:
                    _human_mouse_click(
                        page,
                        target[0],
                        target[1],
                        speed=visual_mouse_speed,
                        hold_ms=visual_click_hold_ms,
                    )
                    _capture_movement("after_maybe_click_text")
                else:
                    locator.click(timeout=timeout_ms)
                actions.append(f"cmd: playwright maybe click text:{step.target}")
                observations.append(f"Maybe clicked text in step {step_num}: {step.target}")
                ui_findings.append(
                    f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}"
                )
                return
            except Exception:
                observations.append(f"Step {step_num}: maybe click not present: {step.target}")
                ui_findings.append(f"step {step_num} verify optional click skipped: {step.target}")
                return

        if step.kind == "bulk_click_in_cards":
            card_selector, required_text = ".track-card", ""
            if "||" in step.value:
                left, right = step.value.split("||", 1)
                card_selector = str(left or ".track-card").strip() or ".track-card"
                required_text = str(right or "").strip()
            seen_selectors: set[str] = set()
            clicked = 0
            no_new_rounds = 0
            for _round in range(1, 18):
                selectors, reached_bottom = _scan_visible_buttons_in_cards(
                    page,
                    card_selector=card_selector,
                    button_selector=step.target,
                    required_text=required_text,
                    seen=seen_selectors,
                )
                if not selectors:
                    no_new_rounds += 1
                for selector in selectors:
                    locator = page.locator(selector).first
                    try:
                        locator.wait_for(state="visible", timeout=timeout_ms)
                    except Exception:
                        continue
                    target = _highlight_target(
                        page,
                        locator,
                        f"step {step_num} BULK",
                        click_pulse_enabled=click_pulse_enabled and visual,
                        show_preview=not (visual and visual_human_mouse),
                    )
                    if target is None:
                        continue
                    if visual and visual_human_mouse and target:
                        _human_mouse_click(
                            page,
                            target[0],
                            target[1],
                            speed=visual_mouse_speed,
                            hold_ms=visual_click_hold_ms,
                        )
                        _capture_movement("after_bulk_click")
                    else:
                        locator.click(timeout=timeout_ms)
                    seen_selectors.add(selector)
                    clicked += 1
                if no_new_rounds >= 2 and reached_bottom:
                    break
                if no_new_rounds >= 3:
                    break
                if reached_bottom and not selectors:
                    break
                _retry_scroll(page, amount=120, pause_ms=160)
            actions.append(
                f"cmd: playwright bulk_click_in_cards selector:{step.target} cards:{card_selector} text:{required_text}"
            )
            observations.append(
                f"Bulk click in cards step {step_num}: selector={step.target}, card={card_selector}, "
                f"text={required_text}, clicked={clicked}"
            )
            ui_findings.append(
                f"step {step_num} verify bulk click in cards: clicked={clicked}, selector={step.target}"
            )
            return

        if step.kind == "bulk_click_until_empty":
            removed = 0
            for _pass in range(1, 24):
                seen: set[str] = set()
                selectors = _scan_visible_selectors(page, button_selector=step.target, seen=seen)
                if not selectors:
                    break
                for selector in selectors:
                    locator = page.locator(selector).first
                    try:
                        locator.wait_for(state="visible", timeout=timeout_ms)
                    except Exception:
                        continue
                    target = _highlight_target(
                        page,
                        locator,
                        f"step {step_num} BULK-EMPTY",
                        click_pulse_enabled=click_pulse_enabled and visual,
                        show_preview=not (visual and visual_human_mouse),
                    )
                    if target is None:
                        continue
                    if visual and visual_human_mouse and target:
                        _human_mouse_click(
                            page,
                            target[0],
                            target[1],
                            speed=visual_mouse_speed,
                            hold_ms=visual_click_hold_ms,
                        )
                        _capture_movement("after_bulk_until_empty_click")
                    else:
                        locator.click(timeout=timeout_ms)
                    removed += 1
                    seen.add(selector)
                try:
                    page.wait_for_timeout(110)
                except Exception:
                    pass
            actions.append(f"cmd: playwright bulk_click_until_empty selector:{step.target}")
            observations.append(f"Bulk click until empty step {step_num}: selector={step.target}, clicked={removed}")
            ui_findings.append(
                f"step {step_num} verify bulk click until empty: clicked={removed}, selector={step.target}"
            )
            return

        if step.kind == "select_label":
            locator = page.locator(step.target).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            target = _highlight_target(
                page,
                locator,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled and visual,
                show_preview=not (visual and visual_human_mouse),
            )
            if target is None:
                raise SystemExit(f"Target occluded or not visible: selector {step.target}")
            if visual:
                if visual_human_mouse and target:
                    _human_mouse_move(page, target[0], target[1], speed=visual_mouse_speed)
                    _capture_movement("after_select_label_move")
            locator.select_option(label=step.value)
            actions.append(f"cmd: playwright select selector:{step.target} label:{step.value}")
            observations.append(
                f"Selected option by label in step {step_num}: selector={step.target}, label={step.value}"
            )
            ui_findings.append(
                f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}"
            )
            return

        if step.kind == "fill_selector":
            locator = page.locator(step.target).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            target = _highlight_target(
                page,
                locator,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled and visual,
                show_preview=not (visual and visual_human_mouse),
            )
            if target is None:
                raise SystemExit(f"Target occluded or not visible: selector {step.target}")
            if visual and visual_human_mouse and target:
                _human_mouse_move(page, target[0], target[1], speed=visual_mouse_speed)
                _capture_movement("after_fill_move")
            locator.fill(step.value, timeout=timeout_ms)
            actions.append(f"cmd: playwright fill selector:{step.target} text:{step.value}")
            observations.append(
                f"Filled input in step {step_num}: selector={step.target}, text={step.value}"
            )
            ui_findings.append(
                f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}"
            )
            return

        if step.kind == "select_value":
            locator = page.locator(step.target).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            target = _highlight_target(
                page,
                locator,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled and visual,
                show_preview=not (visual and visual_human_mouse),
            )
            if target is None:
                raise SystemExit(f"Target occluded or not visible: selector {step.target}")
            if visual:
                if visual_human_mouse and target:
                    _human_mouse_move(page, target[0], target[1], speed=visual_mouse_speed)
                    _capture_movement("after_select_value_move")
            locator.select_option(value=step.value)
            actions.append(f"cmd: playwright select selector:{step.target} value:{step.value}")
            observations.append(
                f"Selected option by value in step {step_num}: selector={step.target}, value={step.value}"
            )
            ui_findings.append(
                f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}"
            )
            return
    finally:
        _restore_iframe_pointer_events(page, iframe_guard)

    raise RuntimeError(f"Unsupported interactive step kind: {step.kind}")


def _apply_wait_step(
    page: Any,
    step: WebStep,
    step_num: int,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    *,
    timeout_ms: int,
) -> None:
    _helpers_apply_wait_step(
        page,
        step,
        step_num,
        actions,
        observations,
        ui_findings,
        timeout_ms=timeout_ms,
        disable_active_youtube_iframe_pointer_events=_disable_active_youtube_iframe_pointer_events,
        force_main_frame_context=_force_main_frame_context,
        restore_iframe_pointer_events=_restore_iframe_pointer_events,
    )


def _launch_browser(
    playwright_obj: Any,
    *,
    visual: bool = False,
    visual_mouse_speed: float = 1.0,
) -> Any:
    kwargs: dict[str, Any] = {"headless": not visual}
    if visual:
        slow_mo = int(max(90, min(500, 260 / max(0.2, visual_mouse_speed))))
        kwargs["slow_mo"] = slow_mo
        kwargs["args"] = [
            "--window-size=1280,860",
            "--window-position=80,60",
        ]
    try:
        return playwright_obj.chromium.launch(channel="chrome", **kwargs)
    except Exception:
        return playwright_obj.chromium.launch(**kwargs)


def _force_visual_overlay_reinstall(page: Any) -> None:
    page.evaluate(
        """
        () => {
          const ids = [
            '__bridge_cursor_overlay',
            '__bridge_trail_layer',
            '__bridge_state_border',
            '__bridge_step_badge',
          ];
          ids.forEach((id) => document.getElementById(id)?.remove());
          window.__bridgeOverlayInstalled = false;
          if (typeof window.__bridgeEnsureOverlay === 'function') {
            window.__bridgeEnsureOverlay();
          }
        }
        """
    )


def _ensure_visual_overlay_ready(page: Any, retries: int = 12, delay_ms: int = 120) -> None:
    last_error: BaseException | None = None
    for _ in range(max(1, retries)):
        try:
            _ensure_visual_overlay_installed(page)
            _verify_visual_overlay_visible(page)
            return
        except BaseException as exc:
            last_error = exc
            try:
                page.wait_for_timeout(delay_ms)
            except Exception:
                pass
    if isinstance(last_error, BaseException):
        raise RuntimeError(str(last_error))
    raise RuntimeError("Visual overlay not visible after retries.")


def _probe_step_target_state(page: Any, step: WebStep) -> dict[str, Any]:
    present: bool | None = None
    visible: bool | None = None
    enabled: bool | None = None
    try:
        if step.kind in {"click_text", "maybe_click_text"}:
            node = page.locator("body").get_by_text(step.target, exact=False).first
        else:
            node = page.locator(step.target).first
        try:
            present = bool(node.count() > 0)
        except Exception:
            present = None
        try:
            visible = bool(node.is_visible(timeout=180))
        except Exception:
            visible = None
        try:
            enabled = bool(node.is_enabled())
        except Exception:
            enabled = None
    except Exception:
        pass
    return {"present": present, "visible": visible, "enabled": enabled}


def _interactive_step_not_applicable_reason(page: Any, step: WebStep) -> str:
    if step.kind not in INTERACTIVE_STEP_KINDS:
        return ""
    state = _probe_step_target_state(page, step)
    if state.get("enabled") is False:
        return (
            "target disabled in current context "
            f"(present={state['present']}, visible={state['visible']}, enabled={state['enabled']})"
        )
    if (
        step.kind in {"click_text", "maybe_click_text"}
        and state.get("present") is False
        and state.get("visible") is False
    ):
        return (
            "target text not present/visible in current context "
            f"(present={state['present']}, visible={state['visible']}, enabled={state['enabled']})"
        )
    return ""


def _is_timeout_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    if "timeout" in name:
        return True
    msg = str(exc).lower()
    return "timeout" in msg and "exceeded" in msg


def _ensure_visual_overlay_ready_best_effort(
    page: Any,
    ui_findings: list[str],
    *,
    cursor_expected: bool,
    retries: int,
    delay_ms: int,
    debug_screenshot_path: Path | None = None,
    force_reinit: bool = False,
) -> bool:
    # Force re-injection / re-enable in attach flows and after navigations.
    last_error: BaseException | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            if force_reinit:
                try:
                    _force_visual_overlay_reinstall(page)
                except BaseException as reinstall_exc:
                    last_error = reinstall_exc
            _ensure_visual_overlay_installed(page)
            if cursor_expected:
                try:
                    _verify_visual_overlay_visible(page)
                    return True
                except BaseException as exc:
                    last_error = exc
                    try:
                        _force_visual_overlay_reinstall(page)
                    except BaseException as reinstall_exc:
                        last_error = reinstall_exc
            else:
                return True
        except BaseException as exc:
            last_error = exc
        try:
            page.wait_for_timeout(delay_ms)
        except Exception:
            pass
        ui_findings.append(f"visual overlay retry {attempt}/{retries}")

    snapshot = _read_visual_overlay_snapshot(page)
    ui_findings.append(f"visual overlay snapshot: {snapshot}")
    if debug_screenshot_path is not None:
        try:
            page.screenshot(path=str(debug_screenshot_path), full_page=True)
            ui_findings.append(f"visual overlay debug screenshot: {_to_repo_rel(debug_screenshot_path)}")
        except Exception as screenshot_exc:
            ui_findings.append(f"visual overlay debug screenshot failed: {screenshot_exc}")
    ui_findings.append(
        "visual overlay degraded: cursor overlay not visible; continuing without cursor"
    )
    if last_error is not None:
        ui_findings.append(f"visual overlay error: {last_error}")
    return False


def _to_repo_rel(path: Path) -> str:
    return str(path.resolve().relative_to(Path.cwd()))
