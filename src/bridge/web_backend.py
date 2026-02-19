"""Deterministic web interaction backend using Playwright."""

from __future__ import annotations

import importlib.util
import json
import math
import random
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
from bridge.web_bulk_scan import (
    scan_playlist_remove_selectors as _bulk_scan_playlist_remove_selectors,
    scan_visible_buttons_in_cards as _bulk_scan_visible_buttons_in_cards,
    scan_visible_ready_add_selectors as _bulk_scan_visible_ready_add_selectors,
    scan_visible_selectors as _bulk_scan_visible_selectors,
    selected_playlist_name as _bulk_selected_playlist_name,
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
from bridge.web_run_finalize import (
    ensure_structured_ui_findings as _finalize_ensure_structured_ui_findings,
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
from bridge.web_steps import (
    WebStep,
    extract_play_track_hints,
    parse_steps,
    rewrite_generic_play_steps,
)
from bridge.web_session import (
    WebSession,
    mark_controlled,
    request_session_state,
)

_LAST_HUMAN_ROUTE: list[tuple[float, float]] = []


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")

_AUTH_HINTS = (
    "cerrar sesion",
    "cerrar sesión",
    "logout",
    "sign out",
    "dashboard",
    "mi cuenta",
    "perfil",
)

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
    play_hints = _extract_play_track_hints(task)
    steps = _rewrite_generic_play_steps(steps, play_hints)

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


def _extract_play_track_hints(task: str) -> list[str]:
    return extract_play_track_hints(task)


def _rewrite_generic_play_steps(steps: list[WebStep], play_hints: list[str]) -> list[WebStep]:
    return rewrite_generic_play_steps(steps, play_hints)


def _playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _safe_page_title(page: object) -> str:
    try:
        # Playwright can throw if execution context is destroyed during navigation/HMR.
        return str(getattr(page, "title")())
    except Exception:
        return ""


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
                    trace_enabled=True,
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

            # Conditional login: if demo button exists+visible+enabled, click; otherwise continue as already authed.
            if _demo_login_button_available(page):
                if _task_already_requests_demo_click(steps):
                    observations.append(
                        "Login step already requested by task; skipping auto demo click insertion"
                    )
                else:
                    observations.append("Login state detected: Entrar demo present and enabled")
                    # Insert a native optional step at the front (keeps evidence before/after machinery).
                    steps = [WebStep("maybe_click_text", "Entrar demo")] + steps
            else:
                observations.append("demo not present; already authed")
                ui_findings.append("demo not present; already authed")

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
                    page.wait_for_timeout(1000)
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
                if teaching_mode and _watchdog_stuck_attempt(
                    attempted_hint or f"watchdog:post-step:{step.kind}"
                ):
                    break
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


def _ensure_structured_ui_findings(
    ui_findings: list[str],
    *,
    result: str,
    where_default: str,
) -> None:
    _finalize_ensure_structured_ui_findings(
        ui_findings,
        result=result,
        where_default=where_default,
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
        if (not _is_specific_selector(step.target)) or _is_stop_semantic_step(step):
            for hint in _semantic_hints_for_selector(step.target):
                candidates.append(WebStep("click_text", hint))
                for selector in _stable_selectors_for_target(hint):
                    candidates.append(WebStep("click_selector", selector))
    elif step.kind == "click_track_play":
        for selector in learning_selectors:
            candidates.insert(0, WebStep("click_selector", selector))

    last_exc: Exception | None = None
    if step.kind == "click_track_play":
        # click_track_play already performs internal paged scanning; avoid triple full rescans.
        total_attempts = 1
    else:
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


def _retry_scroll(page: Any, *, amount: int = 180, pause_ms: int = 140) -> None:
    step = max(80, int(amount))
    try:
        page.evaluate(
            """
            (step) => {
              const main = document.querySelector('main,[role="main"],#main,.main,#__next,.app,[data-testid="main"]');
              if (main && typeof main.scrollBy === 'function') {
                main.scrollBy(0, step);
              }
              window.scrollBy(0, step);
            }
            """,
            step,
        )
    except Exception:
        try:
            page.evaluate("([step]) => window.scrollBy(0, step)", [step])
        except Exception:
            pass
    try:
        page.wait_for_timeout(max(40, int(pause_ms)))
    except Exception:
        pass


def _stable_selectors_for_target(target: str) -> list[str]:
    clean = str(target).strip()
    if not clean:
        return []
    escaped = clean.replace('"', '\\"')
    return [
        f'button:has-text("{escaped}")',
        f'[role="button"]:has-text("{escaped}")',
        f'a:has-text("{escaped}")',
        f'[aria-label*="{escaped}" i]',
        f'[title*="{escaped}" i]',
    ]


def _is_generic_play_label(value: str) -> bool:
    low = str(value or "").strip().lower()
    return low in {"reproducir", "play", "play local"}


def _semantic_hints_for_selector(selector: str) -> list[str]:
    low = str(selector or "").strip().lower()
    if not low:
        return []
    hints: list[str] = []
    if "stop" in low:
        hints.append("Stop")
    if "play" in low or "reproducir" in low:
        hints.append("Reproducir")
    return hints


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
    if "stop" in merged or "#player-stop-btn" in merged:
        return "stop"
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
    if low.startswith("#"):
        return True
    if low.startswith("[data-testid") or low.startswith("[data-test") or low.startswith("[id="):
        return True
    return "__bridge_" not in low and ("track-play-" in low or "player-stop-btn" in low)


def _learned_selectors_for_step(
    step: WebStep,
    selector_map: dict[str, dict[str, list[str]]],
    context: dict[str, str],
) -> list[str]:
    if step.kind not in {"click_text", "click_selector", "click_track_play"}:
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


def _is_stop_semantic_step(step: WebStep) -> bool:
    if step.kind not in {"click_selector", "click_text", "maybe_click_text"}:
        return False
    probe = f"{step.kind}:{step.target}".lower()
    return "stop" in probe or "#player-stop-btn" in probe


def _should_soft_skip_wait_timeout(
    *, steps: list[WebStep], idx: int, step: WebStep, teaching_mode: bool
) -> bool:
    if not teaching_mode or step.kind != "wait_text":
        return False
    target = str(step.target).strip().lower()
    if "now playing" not in target:
        return False
    remaining = steps[idx:]
    return any(_is_stop_semantic_step(candidate) for candidate in remaining)


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
    msg = f"Me he atascado en: {step_text}. Te cedo el control para que me ayudes."
    try:
        page.evaluate(
            """
            ([message]) => {
              const id = '__bridge_teaching_handoff_notice';
              let el = document.getElementById(id);
              if (!el) {
                el = document.createElement('div');
                el.id = id;
                el.style.position = 'fixed';
                el.style.left = '50%';
                el.style.bottom = '18px';
                el.style.transform = 'translateX(-50%)';
                el.style.padding = '10px 14px';
                el.style.borderRadius = '10px';
                el.style.background = 'rgba(245,158,11,0.95)';
                el.style.color = '#fff';
                el.style.font = '13px/1.3 monospace';
                el.style.zIndex = '2147483647';
                el.style.boxShadow = '0 8px 18px rgba(0,0,0,0.3)';
                document.documentElement.appendChild(el);
              }
              el.textContent = String(message || '');
            }
            """,
            [msg],
        )
    except Exception:
        return


def _show_custom_handoff_notice(page: Any, message: str) -> None:
    try:
        page.evaluate(
            """
            ([msg]) => {
              const id = '__bridge_teaching_handoff_notice';
              let el = document.getElementById(id);
              if (!el) {
                el = document.createElement('div');
                el.id = id;
                el.style.position = 'fixed';
                el.style.left = '50%';
                el.style.bottom = '18px';
                el.style.transform = 'translateX(-50%)';
                el.style.padding = '10px 14px';
                el.style.borderRadius = '10px';
                el.style.background = 'rgba(245,158,11,0.95)';
                el.style.color = '#fff';
                el.style.font = '13px/1.3 monospace';
                el.style.zIndex = '2147483647';
                el.style.boxShadow = '0 8px 18px rgba(0,0,0,0.3)';
                document.documentElement.appendChild(el);
              }
              el.textContent = String(msg || '');
            }
            """,
            [message],
        )
    except Exception:
        return


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
    if notice_message:
        _show_custom_handoff_notice(page, notice_message)
    else:
        _show_stuck_handoff_notice(page, where)
    _set_learning_handoff_overlay(page, True)
    if visual and control_enabled:
        _set_assistant_control_overlay(page, False)
    if session is not None:
        mark_controlled(session, False, url=getattr(page, "url", ""), title=_safe_page_title(page))
        _notify_learning_state(session, active=True, window_seconds=learning_window_seconds)
        try:
            _update_top_bar_state(
                page,
                _session_state_payload(session, override_controlled=False, learning_active=True),
            )
        except Exception:
            pass
    if "cmd: playwright release control (teaching handoff)" not in actions:
        actions.append("cmd: playwright release control (teaching handoff)")
    ui_findings.append(
        notice_message or f"Me he atascado en: {where}. Te cedo el control para que me ayudes."
    )
    if "control released" not in ui_findings:
        ui_findings.append("control released")
    ui_findings.append(f"what_failed={what_failed}")
    ui_findings.append(f"where={where}")
    ui_findings.append(f"attempted={attempted or 'watchdog'}")
    ui_findings.append("next_best_action=human_assist")
    ui_findings.append(f"why_likely={why_likely}")
    return False


def _is_iframe_focus_locked(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const active = document.activeElement;
                  if (!active) return false;
                  if (String(active.tagName || '').toUpperCase() === 'IFRAME') return true;
                  return !!document.querySelector('iframe:focus,iframe:focus-within');
                }
                """
            )
        )
    except Exception:
        return False


def _disable_active_youtube_iframe_pointer_events(page: Any) -> dict[str, Any] | None:
    if _page_is_closed(page):
        return None
    try:
        token = page.evaluate(
            """
            () => {
              const active = document.activeElement;
              let frame = null;
              if (active && String(active.tagName || '').toUpperCase() === 'IFRAME') {
                frame = active;
              }
              if (!frame) frame = document.querySelector('iframe:focus,iframe:focus-within');
              if (!frame) return null;
              const src = String(frame.getAttribute('src') || '').toLowerCase();
              const isYoutube =
                src.includes('youtube.com') ||
                src.includes('youtube-nocookie.com') ||
                src.includes('youtu.be');
              if (!isYoutube) return null;
              const prev = String(frame.style.pointerEvents || '');
              frame.setAttribute('data-bridge-prev-pe', prev || '__EMPTY__');
              frame.style.pointerEvents = 'none';
              const all = Array.from(document.querySelectorAll('iframe'));
              const idx = all.indexOf(frame);
              return { idx, id: String(frame.id || ''), prev };
            }
            """
        )
    except Exception:
        return None
    return token if isinstance(token, dict) else None


def _restore_iframe_pointer_events(page: Any, token: dict[str, Any] | None) -> None:
    if not token or _page_is_closed(page):
        return
    try:
        page.evaluate(
            """
            ([tok]) => {
              if (!tok || typeof tok !== 'object') return;
              const all = Array.from(document.querySelectorAll('iframe'));
              let frame = null;
              if (tok.id) frame = document.getElementById(String(tok.id));
              if (!frame && Number.isInteger(tok.idx) && tok.idx >= 0 && tok.idx < all.length) {
                frame = all[tok.idx];
              }
              if (!frame) return;
              const prevAttr = frame.getAttribute('data-bridge-prev-pe');
              const prev = prevAttr === '__EMPTY__' ? '' : String(prevAttr || tok.prev || '');
              frame.style.pointerEvents = prev;
              frame.removeAttribute('data-bridge-prev-pe');
            }
            """,
            [token],
        )
    except Exception:
        return


def _force_main_frame_context(page: Any, max_seconds: float = 8.0) -> bool:
    # Main-frame-first policy: escape iframe focus and re-anchor to document body before actions.
    deadline = time.monotonic() + max(0.1, float(max_seconds))
    while time.monotonic() <= deadline:
        try:
            page.evaluate(
                """
                () => {
                  const active = document.activeElement;
                  if (active && String(active.tagName || '').toUpperCase() === 'IFRAME') {
                    try { active.blur(); } catch (_e) {}
                  }
                }
                """
            )
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            page.evaluate(
                """
                () => {
                  if (!document.body) return false;
                  if (typeof document.body.focus === 'function') document.body.focus();
                  try {
                    const evt = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
                    document.body.dispatchEvent(evt);
                  } catch (_e) {}
                  return true;
                }
                """
            )
        except Exception:
            pass
        try:
            is_main = bool(page.evaluate("() => !!document.body && window === window.top"))
        except Exception:
            is_main = False
        if is_main and not _is_iframe_focus_locked(page):
            return True
        try:
            page.wait_for_timeout(120)
        except Exception:
            pass
    return False


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


def _selected_playlist_name(page: Any) -> str:
    return _bulk_selected_playlist_name(page)


def _scan_visible_ready_add_selectors(page: Any, seen: set[str]) -> tuple[list[str], bool]:
    return _bulk_scan_visible_ready_add_selectors(page, seen)


def _scan_playlist_remove_selectors(page: Any, seen: set[str]) -> tuple[list[str], bool]:
    return _bulk_scan_playlist_remove_selectors(page, seen)


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
            if not visual:
                return
            if movement_capture_dir is None or evidence_paths is None:
                return
            move_capture_count += 1
            shot = movement_capture_dir / f"step_{step_num}_move_{move_capture_count}_{tag}.png"
            try:
                pts = list(_LAST_HUMAN_ROUTE)
                vw_vh = page.evaluate(
                    "() => ({w: window.innerWidth || 1280, h: window.innerHeight || 860})"
                )
                if isinstance(pts, list) and len(pts) >= 2:
                    w = int((vw_vh or {}).get("w") or 1280)
                    h = int((vw_vh or {}).get("h") or 860)
                    clean_pts: list[tuple[float, float]] = []
                    for p in pts:
                        if isinstance(p, (list, tuple)) and len(p) >= 2:
                            try:
                                clean_pts.append((float(p[0]), float(p[1])))
                            except Exception:
                                continue
                    if len(clean_pts) >= 2:
                        svg_path = movement_capture_dir / f"step_{step_num}_move_{move_capture_count}_{tag}.svg"
                        points_attr = " ".join(f"{x:.2f},{y:.2f}" for x, y in clean_pts)
                        svg = (
                            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
                            f'viewBox="0 0 {w} {h}">'
                            f'<polyline fill="none" stroke="rgb(0,180,255)" stroke-width="6" '
                            f'stroke-linecap="round" stroke-linejoin="round" points="{points_attr}" />'
                            "</svg>\n"
                        )
                        svg_path.write_text(svg, encoding="utf-8")
                        evidence_paths.append(_to_repo_rel(svg_path))
                page.evaluate(
                    """
                    () => {
                      const prev = document.getElementById('__bridge_capture_path');
                      if (prev) prev.remove();
                      const pts = window.__bridgeLastHumanRoute;
                      if (!Array.isArray(pts) || pts.length < 2) return;
                      const clean = pts
                        .map((p) => Array.isArray(p) ? { x: Number(p[0]), y: Number(p[1]) } : null)
                        .filter((p) => p && Number.isFinite(p.x) && Number.isFinite(p.y));
                      if (clean.length < 2) return;
                      const svgNS = 'http://www.w3.org/2000/svg';
                      const svg = document.createElementNS(svgNS, 'svg');
                      svg.id = '__bridge_capture_path';
                      svg.setAttribute('width', '100%');
                      svg.setAttribute('height', '100%');
                      svg.setAttribute(
                        'viewBox',
                        `0 0 ${Math.max(1, window.innerWidth || 1)} ${Math.max(1, window.innerHeight || 1)}`
                      );
                      svg.setAttribute('preserveAspectRatio', 'none');
                      svg.style.position = 'fixed';
                      svg.style.inset = '0';
                      svg.style.pointerEvents = 'none';
                      svg.style.zIndex = '2147483646';
                      const poly = document.createElementNS(svgNS, 'polyline');
                      poly.setAttribute('fill', 'none');
                      poly.setAttribute('stroke', 'rgba(0,180,255,1)');
                      poly.setAttribute('stroke-width', '8');
                      poly.setAttribute('stroke-linecap', 'round');
                      poly.setAttribute('stroke-linejoin', 'round');
                      poly.setAttribute('points', clean.map((p) => `${p.x},${p.y}`).join(' '));
                      svg.appendChild(poly);
                      document.documentElement.appendChild(svg);
                    }
                    """
                )
                page.wait_for_timeout(50)
                page.screenshot(path=str(shot), full_page=False)
                page.evaluate("() => document.getElementById('__bridge_capture_path')?.remove()")
                evidence_paths.append(_to_repo_rel(shot))
            except Exception:
                return

        def _is_generic_play_click(target_text: str) -> bool:
            return _is_generic_play_label(target_text)

        def _scan_whole_page_for_play_buttons() -> int:
            # Force full-page scan before generic play clicks. This avoids clicking the first visible
            # "Reproducir" without disambiguation when multiple tracks are present.
            total = 0
            try:
                page.evaluate("() => window.scrollTo(0, 0)")
            except Exception:
                pass
            for _ in range(18):
                try:
                    total = int(
                        page.evaluate(
                            """
                            () => document.querySelectorAll(
                              "[id^='track-play-'], [data-testid^='track-play-'], .track-card button"
                            ).length
                            """
                        )
                    )
                except Exception:
                    total = 0
                try:
                    moved = bool(
                        page.evaluate(
                            """
                            () => {
                              const maxY = Math.max(
                                0,
                                (document.documentElement?.scrollHeight || 0) - window.innerHeight
                              );
                              const prev = window.scrollY || 0;
                              const next = Math.min(maxY, prev + Math.max(130, Math.floor(window.innerHeight * 0.28)));
                              window.scrollTo(0, next);
                              return next > prev;
                            }
                            """
                        )
                    )
                except Exception:
                    moved = False
                if not moved:
                    break
                try:
                    page.wait_for_timeout(95)
                except Exception:
                    pass
            try:
                page.evaluate("() => window.scrollTo(0, 0)")
            except Exception:
                pass
            return total

        if step.kind == "click_track_play":
            track_hint = _collapse_ws(step.target)
            card = None
            try:
                page.evaluate("() => window.scrollTo(0, 0)")
                page.wait_for_timeout(80)
            except Exception:
                pass
            max_scan_pages = 14
            for page_idx in range(max_scan_pages):
                candidate = page.locator(".track-card").filter(has_text=track_hint).first
                try:
                    if candidate.count() > 0:
                        in_viewport = bool(
                            candidate.evaluate(
                                """
                                (el) => {
                                  const r = el.getBoundingClientRect();
                                  return !!r && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
                                }
                                """
                            )
                        )
                        if in_viewport:
                            card = candidate
                            observations.append(f"scan page {page_idx + 1}: track card visible for '{track_hint}'")
                            break
                except Exception:
                    pass
                observations.append(f"scan page {page_idx + 1}: track card not visible yet for '{track_hint}'")
                _retry_scroll(page, amount=120, pause_ms=160)
            if card is None:
                # Final deep scroll pass: sometimes one extra page down is enough.
                try:
                    page.evaluate("() => window.scrollBy(0, Math.max(220, Math.floor(window.innerHeight * 0.45)))")
                    page.wait_for_timeout(170)
                except Exception:
                    pass
                candidate = page.locator(".track-card").filter(has_text=track_hint).first
                try:
                    if candidate.count() > 0:
                        card = candidate
                        observations.append(f"scan final pass: found card for '{track_hint}'")
                except Exception:
                    pass
            if card is None:
                raise RuntimeError(f"Track card not found for play target: {track_hint}")
            local_btn = card.locator('[id^="track-play-local-"], [data-testid^="track-play-local-"]').first
            play_btn = card.locator('[id^="track-play-"], [data-testid^="track-play-"]').first
            button = None
            try:
                if local_btn.count() > 0:
                    button = local_btn
            except Exception:
                button = None
            if button is None:
                button = play_btn
            button.wait_for(state="visible", timeout=timeout_ms)
            target = _highlight_target(
                page,
                button,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled and visual,
                show_preview=not (visual and visual_human_mouse),
                auto_scroll=False,
            )
            if target is None:
                raise SystemExit(f"Play button occluded for track: {track_hint}")
            if visual and visual_human_mouse and target:
                _human_mouse_click(
                    page,
                    target[0],
                    target[1],
                    speed=visual_mouse_speed,
                    hold_ms=visual_click_hold_ms,
                )
                _capture_movement("after_click_track_play")
            else:
                button.click(timeout=timeout_ms)
            chosen_selector = ""
            try:
                chosen_selector = str(
                    button.get_attribute("id")
                    or button.get_attribute("data-testid")
                    or ""
                ).strip()
            except Exception:
                chosen_selector = ""
            actions.append(
                f"cmd: playwright click track_play:{track_hint}"
                + (f" selector:#{chosen_selector}" if chosen_selector and not chosen_selector.startswith("#") else "")
            )
            observations.append(
                f"Clicked track card play in step {step_num}: track={track_hint}"
                + (f", selector={chosen_selector}" if chosen_selector else "")
            )
            ui_findings.append(
                f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}"
            )
            return

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
            if _is_generic_play_click(step.target):
                play_candidates = _scan_whole_page_for_play_buttons()
                if play_candidates > 1:
                    ui_findings.append(
                        f"step {step_num} blocked ambiguous generic play click: candidates={play_candidates}"
                    )
                    raise RuntimeError(
                        "Ambiguous generic play click detected. Specify track selector before clicking Reproducir."
                    )
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
                if _is_login_target(step.target) and _looks_authenticated(page):
                    observations.append(
                        f"Step {step_num}: target '{step.target}' not found; authenticated state detected."
                    )
                    ui_findings.append(
                        f"step {step_num} verify authenticated session already active"
                    )
                    return
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

        if step.kind == "add_all_ready_to_playlist":
            selected_playlist = _selected_playlist_name(page)
            if not selected_playlist:
                raise RuntimeError("No playlist selected. Select playlist before adding READY tracks.")
            observations.append(f"step {step_num}: target playlist selected -> {selected_playlist}")
            try:
                page.evaluate("() => window.scrollTo(0, 0)")
                page.wait_for_timeout(90)
            except Exception:
                pass
            seen_selectors: set[str] = set()
            added = 0
            no_new_rounds = 0
            for _round in range(1, 18):
                selectors, reached_bottom = _scan_visible_ready_add_selectors(page, seen_selectors)
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
                        f"step {step_num} READY",
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
                        _capture_movement("after_add_ready_click")
                    else:
                        locator.click(timeout=timeout_ms)
                    seen_selectors.add(selector)
                    added += 1
                if no_new_rounds >= 2 and reached_bottom:
                    break
                if no_new_rounds >= 3:
                    break
                if reached_bottom and not selectors:
                    break
                _retry_scroll(page, amount=120, pause_ms=160)
            actions.append(f"cmd: playwright add_all_ready_to_playlist selected:{selected_playlist}")
            observations.append(
                f"Added READY tracks in step {step_num}: count={added}, playlist={selected_playlist}"
            )
            ui_findings.append(
                f"step {step_num} verify added READY tracks: count={added}, playlist={selected_playlist}"
            )
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

        if step.kind == "remove_all_playlist_tracks":
            selected_playlist = _selected_playlist_name(page)
            if not selected_playlist:
                raise RuntimeError("No playlist selected. Select playlist before removing tracks.")
            observations.append(f"step {step_num}: removing all tracks from playlist -> {selected_playlist}")
            bulk_step = WebStep("bulk_click_until_empty", '[id^="playlist-track-remove-"]')
            _apply_interactive_step(
                page,
                bulk_step,
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
                movement_capture_dir=movement_capture_dir,
                evidence_paths=evidence_paths,
            )
            actions.append(f"cmd: playwright remove_all_playlist_tracks selected:{selected_playlist}")
            ui_findings.append(
                f"step {step_num} verify removed playlist tracks: playlist={selected_playlist}"
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
    iframe_guard = _disable_active_youtube_iframe_pointer_events(page)
    if not _force_main_frame_context(page):
        _restore_iframe_pointer_events(page, iframe_guard)
        raise RuntimeError("Unable to enforce main frame context for wait step")
    try:
        if step.kind == "wait_selector":
            actions.append(f"cmd: playwright wait selector:{step.target}")
            page.wait_for_selector(step.target, timeout=timeout_ms)
            observations.append(f"Wait selector step {step_num}: {step.target}")
            ui_findings.append(f"step {step_num} verify selector visible: {step.target}")
            return
        if step.kind == "wait_text":
            actions.append(f"cmd: playwright wait text:{step.target}")
            page.locator("body").get_by_text(step.target, exact=False).first.wait_for(
                state="visible", timeout=timeout_ms
            )
            observations.append(f"Wait text step {step_num}: {step.target}")
            ui_findings.append(f"step {step_num} verify text visible: {step.target}")
            return
    finally:
        _restore_iframe_pointer_events(page, iframe_guard)
    raise RuntimeError(f"Unsupported wait step kind: {step.kind}")


def _is_login_target(text: str) -> bool:
    low = text.lower().strip()
    return low in ("entrar demo", "entrar", "login", "sign in", "iniciar sesión")


def _task_already_requests_demo_click(steps: list[WebStep]) -> bool:
    for step in steps:
        if step.kind in ("click_text", "maybe_click_text") and _is_login_target(step.target):
            return True
    return False


def _looks_authenticated(page: Any) -> bool:
    try:
        if page.locator(".track-card").first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    for hint in _AUTH_HINTS:
        try:
            if page.get_by_text(hint, exact=False).count() > 0:
                return True
        except Exception:
            continue
    return False


def _launch_browser(
    playwright_obj: Any,
    *,
    visual: bool = False,
    visual_mouse_speed: float = 1.0,
) -> Any:
    kwargs: dict[str, Any] = {"headless": not visual}
    if visual:
        slow_mo = int(max(180, min(500, 260 / max(0.2, visual_mouse_speed))))
        kwargs["slow_mo"] = slow_mo
        kwargs["args"] = [
            "--window-size=1280,860",
            "--window-position=80,60",
        ]
    try:
        return playwright_obj.chromium.launch(channel="chrome", **kwargs)
    except Exception:
        return playwright_obj.chromium.launch(**kwargs)


def _install_visual_overlay(
    page: Any,
    *,
    cursor_enabled: bool,
    click_pulse_enabled: bool,
    scale: float,
    color: str,
    trace_enabled: bool,
    session_state: dict[str, Any] | None = None,
) -> None:
    config = {
        "cursorEnabled": bool(cursor_enabled),
        "clickPulseEnabled": bool(click_pulse_enabled),
        "scale": float(scale),
        "color": str(color),
        "traceEnabled": bool(trace_enabled),
    }
    session_json = json.dumps(session_state or {}, ensure_ascii=False)
    script_template = """
    (() => {
      const cfg = __CFG_JSON__;
      const sessionState = __SESSION_JSON__;
      const installOverlay = () => {
        const prevCfgRaw = window.__bridgeOverlayConfig || null;
        const cfgRaw = JSON.stringify(cfg || {});
        const prevRaw = JSON.stringify(prevCfgRaw || {});
        if (window.__bridgeOverlayInstalled && prevRaw !== cfgRaw) {
          const ids = [
            '__bridge_cursor_overlay',
            '__bridge_trail_layer',
            '__bridge_state_border',
            '__bridge_step_badge',
          ];
          ids.forEach((id) => document.getElementById(id)?.remove());
          window.__bridgeOverlayInstalled = false;
        }
        if (window.__bridgeOverlayInstalled) return true;
        window.__bridgeOverlayConfig = cfg;
        const root = document.documentElement;
        const body = document.body;
        if (!root || !body) {
          if (!window.__bridgeOverlayRetryAttached) {
            window.__bridgeOverlayRetryAttached = true;
            document.addEventListener('DOMContentLoaded', () => {
              installOverlay();
            }, { once: true });
          }
          return false;
        }
        const overlayHost = body;
        const cursor = document.createElement('div');
        cursor.id = '__bridge_cursor_overlay';
        cursor.style.position = 'fixed';
        cursor.style.width = `${14 * cfg.scale}px`;
        cursor.style.height = `${14 * cfg.scale}px`;
        cursor.style.border = `${2 * cfg.scale}px solid ${cfg.color}`;
        cursor.style.borderRadius = '50%';
        cursor.style.boxShadow = `0 0 0 ${3 * cfg.scale}px rgba(59,167,255,0.25)`;
        cursor.style.pointerEvents = 'none';
        cursor.style.zIndex = '2147483647';
        cursor.style.background = 'rgba(59,167,255,0.15)';
        cursor.style.display = cfg.cursorEnabled ? 'block' : 'none';
        cursor.style.transition = 'width 120ms ease, height 120ms ease, left 80ms linear, top 80ms linear';
        overlayHost.appendChild(cursor);
        const trailLayer = document.createElement('div');
        trailLayer.id = '__bridge_trail_layer';
        trailLayer.style.position = 'fixed';
        trailLayer.style.inset = '0';
        trailLayer.style.pointerEvents = 'none';
        trailLayer.style.zIndex = '2147483646';
        overlayHost.appendChild(trailLayer);

        const stateBorder = document.createElement('div');
        stateBorder.id = '__bridge_state_border';
        stateBorder.style.position = 'fixed';
        stateBorder.style.inset = '0';
        stateBorder.style.pointerEvents = 'none';
        stateBorder.style.zIndex = '2147483642';
        stateBorder.style.boxSizing = 'border-box';
        stateBorder.style.borderRadius = String(14 * cfg.scale) + 'px';
        stateBorder.style.border = String(6 * cfg.scale) + 'px solid rgba(210,210,210,0.22)';
        stateBorder.style.boxShadow = '0 0 0 1px rgba(0,0,0,0.28) inset';
        stateBorder.style.transition =
          'border-color 180ms ease-out, box-shadow 180ms ease-out, ' +
          'border-width 180ms ease-out';
        overlayHost.appendChild(stateBorder);

        window.__bridgeSetStateBorder = (state) => {
          const s = state || {};
          const controlled = !!s.controlled;
          const open = String(s.state || 'open') === 'open';
          const incidentOpen = !!s.incident_open;
          const learningActive = !!s.learning_active;
          const controlUrl = window.__bridgeResolveControlUrl ? window.__bridgeResolveControlUrl(s) : null;
          const agentOnline = !!controlUrl && s.agent_online !== false;
          const readyManual = open && !controlled && agentOnline && !incidentOpen && !learningActive;

          let color = 'rgba(210,210,210,0.22)';
          let glow = '0 0 0 1px rgba(0,0,0,0.28) inset';
          if (!open) {
            color = 'rgba(40,40,40,0.55)';
            glow = '0 0 0 1px rgba(0,0,0,0.35) inset';
          } else if (controlled) {
            color = 'rgba(59,167,255,0.95)';
            glow = '0 0 0 2px rgba(59,167,255,0.35) inset, 0 0 26px rgba(59,167,255,0.22)';
          } else if (incidentOpen) {
            color = 'rgba(255,82,82,0.95)';
            glow = '0 0 0 2px rgba(255,82,82,0.32) inset, 0 0 26px rgba(255,82,82,0.18)';
          } else if (learningActive) {
            color = 'rgba(245,158,11,0.95)';
            glow = '0 0 0 2px rgba(245,158,11,0.30) inset, 0 0 26px rgba(245,158,11,0.18)';
          } else if (readyManual) {
            color = 'rgba(34,197,94,0.95)';
            glow = '0 0 0 2px rgba(34,197,94,0.32) inset, 0 0 26px rgba(34,197,94,0.18)';
          } else {
            color = 'rgba(210,210,210,0.22)';
            glow = '0 0 0 1px rgba(0,0,0,0.28) inset';
          }

          const emphasized = (controlled || incidentOpen || readyManual);
          stateBorder.style.borderWidth = String((emphasized ? 10 : 6) * cfg.scale) + 'px';
          stateBorder.style.borderColor = color;
          stateBorder.style.boxShadow = glow;
          window.__bridgeOverlayState = {
            controlled,
            incidentOpen,
            learningActive,
            readyManual,
          };
        };

        let lastTrailPoint = null;
        const emitTrail = (x, y) => {
        if (!cfg.cursorEnabled) return;
        const px = Number(x);
        const py = Number(y);
        if (!Number.isFinite(px) || !Number.isFinite(py)) return;
        if (lastTrailPoint && Number.isFinite(lastTrailPoint.x) && Number.isFinite(lastTrailPoint.y)) {
          const dx = px - lastTrailPoint.x;
          const dy = py - lastTrailPoint.y;
          const len = Math.hypot(dx, dy);
          if (len >= 1.5) {
            const seg = document.createElement('div');
            seg.style.position = 'fixed';
            seg.style.left = `${lastTrailPoint.x}px`;
            seg.style.top = `${lastTrailPoint.y}px`;
            seg.style.width = `${len}px`;
            seg.style.height = '4px';
            seg.style.transformOrigin = '0 50%';
            seg.style.transform = `rotate(${Math.atan2(dy, dx)}rad)`;
            seg.style.borderRadius = '999px';
            seg.style.background = 'rgba(0,180,255,1)';
            seg.style.boxShadow = '0 0 10px rgba(0,180,255,1)';
            seg.style.pointerEvents = 'none';
            seg.style.opacity = '0.95';
            seg.style.transition = 'opacity 5000ms linear';
            trailLayer.appendChild(seg);
            requestAnimationFrame(() => { seg.style.opacity = '0'; });
            setTimeout(() => seg.remove(), 5100);
          }
        }
        const dot = document.createElement('div');
        dot.style.position = 'fixed';
        dot.style.left = `${Math.max(0, px - 2.5)}px`;
        dot.style.top = `${Math.max(0, py - 2.5)}px`;
        dot.style.width = '7px';
        dot.style.height = '7px';
        dot.style.borderRadius = '50%';
        dot.style.background = 'rgba(0,180,255,1)';
        dot.style.pointerEvents = 'none';
        dot.style.opacity = '0.95';
        dot.style.transition = 'opacity 5000ms linear';
        trailLayer.appendChild(dot);
        requestAnimationFrame(() => { dot.style.opacity = '0'; });
        setTimeout(() => dot.remove(), 5100);
        lastTrailPoint = { x: px, y: py };
        };

        const normalizePoint = (x, y) => {
        const nx = Number(x);
        const ny = Number(y);
        if (!Number.isFinite(nx) || !Number.isFinite(ny)) return null;
        const w = window.innerWidth || 0;
        const h = window.innerHeight || 0;
        const cx = Math.max(0, w > 0 ? Math.min(w - 1, nx) : nx);
        const cy = Math.max(0, h > 0 ? Math.min(h - 1, ny) : ny);
        // Ignore noisy top-left synthetic points when we already have a stable cursor position.
        if (cx <= 1 && cy <= 1 && window.__bridgeCursorPos) {
          return { x: window.__bridgeCursorPos.x, y: window.__bridgeCursorPos.y };
        }
        return { x: cx, y: cy };
        };

        const setCursor = (x, y) => {
        const p = normalizePoint(x, y);
        if (!p) return;
        x = p.x;
        y = p.y;
        const normal = 14 * cfg.scale;
        window.__bridgeCursorPos = { x, y };
        cursor.style.width = `${normal}px`;
        cursor.style.height = `${normal}px`;
        cursor.style.left = `${Math.max(0, x - normal / 2)}px`;
        cursor.style.top = `${Math.max(0, y - normal / 2)}px`;
        };

        window.__bridgeGetCursorPos = () => {
        const pos = window.__bridgeCursorPos || null;
        if (!pos || typeof pos.x !== 'number' || typeof pos.y !== 'number') return null;
        return { x: pos.x, y: pos.y };
        };

        window.addEventListener('mousemove', (ev) => {
        if (!cfg.cursorEnabled) return;
        const st = window.__bridgeOverlayState || null;
        // Ignore native mousemove noise while assistant is driving the page.
        if (st && st.controlled) return;
        setCursor(ev.clientX, ev.clientY);
        emitTrail(ev.clientX, ev.clientY);
        }, true);

        window.__bridgeMoveCursor = (x, y) => {
        if (!cfg.cursorEnabled) return;
        const p = normalizePoint(x, y);
        if (!p) return;
        setCursor(p.x, p.y);
        emitTrail(p.x, p.y);
        };

        const initialPos = (() => {
          const prev = window.__bridgeCursorPos;
          if (prev && typeof prev.x === 'number' && typeof prev.y === 'number') {
            return { x: prev.x, y: prev.y };
          }
          const w = window.innerWidth || 0;
          const h = window.innerHeight || 0;
          return { x: Math.max(12, w * 0.5), y: Math.max(12, h * 0.5) };
        })();
        setCursor(initialPos.x, initialPos.y);

        window.__bridgeShowClick = (x, y, label) => {
        const p = normalizePoint(x, y);
        if (!p) return;
        x = p.x;
        y = p.y;
        if (cfg.cursorEnabled) {
          window.__bridgeMoveCursor(x, y);
        }
        if (cfg.clickPulseEnabled) {
          window.__bridgePulseAt(x, y);
        }
        if (label) {
          let badge = document.getElementById('__bridge_step_badge');
          if (!badge) {
            badge = document.createElement('div');
            badge.id = '__bridge_step_badge';
            badge.style.position = 'fixed';
            badge.style.zIndex = '2147483647';
            badge.style.padding = '4px 8px';
            badge.style.borderRadius = '6px';
            badge.style.font = '12px/1.2 monospace';
            badge.style.background = '#111';
            badge.style.color = '#fff';
            badge.style.pointerEvents = 'none';
            document.documentElement.appendChild(badge);
          }
          badge.textContent = label;
          badge.style.left = `${Math.max(0, x + 14)}px`;
          badge.style.top = `${Math.max(0, y - 8)}px`;
        }
        };

        window.__bridgePulseAt = (x, y) => {
        if (!cfg.clickPulseEnabled) return;
        const p = normalizePoint(x, y);
        if (!p) return;
        x = p.x;
        y = p.y;
        const normal = 14 * cfg.scale;
        const click = 22 * cfg.scale;
        if (cfg.cursorEnabled) {
          cursor.style.width = `${click}px`;
          cursor.style.height = `${click}px`;
          cursor.style.left = `${Math.max(0, x - click / 2)}px`;
          cursor.style.top = `${Math.max(0, y - click / 2)}px`;
          setTimeout(() => {
            cursor.style.width = `${normal}px`;
            cursor.style.height = `${normal}px`;
            cursor.style.left = `${Math.max(0, x - normal / 2)}px`;
            cursor.style.top = `${Math.max(0, y - normal / 2)}px`;
          }, 200);
        }
        const ring = document.createElement('div');
        ring.style.position = 'fixed';
        ring.style.left = `${Math.max(0, x - 10)}px`;
        ring.style.top = `${Math.max(0, y - 10)}px`;
        ring.style.width = '20px';
        ring.style.height = '20px';
        ring.style.borderRadius = '50%';
        ring.style.border = `2px solid ${cfg.color}`;
        ring.style.opacity = '0.9';
        ring.style.pointerEvents = 'none';
        ring.style.zIndex = '2147483647';
        ring.style.transform = 'scale(0.7)';
        ring.style.transition = 'transform 650ms ease, opacity 650ms ease';
        document.documentElement.appendChild(ring);
        requestAnimationFrame(() => {
          ring.style.transform = 'scale(2.1)';
          ring.style.opacity = '0';
        });
        setTimeout(() => ring.remove(), 720);
        };

        window.__bridgeDrawPath = (points) => {
        if (!cfg.cursorEnabled) return;
        if (!Array.isArray(points) || points.length < 2) return;
        const clean = points
          .map((p) => Array.isArray(p) ? { x: Number(p[0]), y: Number(p[1]) } : null)
          .filter((p) => p && Number.isFinite(p.x) && Number.isFinite(p.y));
        if (clean.length < 2) return;
        const svgNS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('width', '100%');
        svg.setAttribute('height', '100%');
        svg.setAttribute(
          'viewBox',
          `0 0 ${Math.max(1, window.innerWidth || 1)} ${Math.max(1, window.innerHeight || 1)}`
        );
        svg.style.position = 'fixed';
        svg.style.inset = '0';
        svg.style.pointerEvents = 'none';
        svg.style.zIndex = '2147483646';
        svg.style.overflow = 'visible';
        svg.style.opacity = '0.98';
        svg.style.transition = 'opacity 5000ms linear';
        const poly = document.createElementNS(svgNS, 'polyline');
        poly.setAttribute('fill', 'none');
        poly.setAttribute('stroke', 'rgba(0,180,255,1)');
        poly.setAttribute('stroke-width', '4');
        poly.setAttribute('stroke-linecap', 'round');
        poly.setAttribute('stroke-linejoin', 'round');
        poly.setAttribute('points', clean.map((p) => `${p.x},${p.y}`).join(' '));
        svg.appendChild(poly);
        trailLayer.appendChild(svg);
        requestAnimationFrame(() => { svg.style.opacity = '0'; });
        setTimeout(() => svg.remove(), 5100);
        };
        window.__bridgeResolveControlUrl = (state) => {
          const s = state || {};
          if (s.control_url && typeof s.control_url === 'string') return s.control_url;
          const p = Number(s.control_port || 0);
          if (p > 0) return `http://127.0.0.1:${p}`;
          return '';
        };
        window.__bridgeSetTopBarVisible = (visible) => {
          const bar = document.getElementById('__bridge_session_top_bar');
          if (!bar) return;
          if (visible) {
            bar.dataset.visible = '1';
            bar.style.transform = 'translateY(0)';
            bar.style.opacity = '1';
          } else {
            bar.dataset.visible = '0';
            bar.style.transform = 'translateY(-110%)';
            bar.style.opacity = '0';
          }
        };
        window.__bridgeSetIncidentOverlay = (enabled, message) => {
          const id = '__bridge_incident_overlay';
          const existing = document.getElementById(id);
          if (!enabled) {
            if (existing) existing.remove();
            return;
          }
          if (existing) {
            const badge = existing.querySelector('[data-role="badge"]');
            if (badge) badge.textContent = message || 'INCIDENT DETECTED';
            return;
          }
          const wrap = document.createElement('div');
          wrap.id = id;
          wrap.style.position = 'fixed';
          wrap.style.inset = '0';
          wrap.style.border = '3px solid #ff5252';
          wrap.style.boxSizing = 'border-box';
          wrap.style.pointerEvents = 'none';
          wrap.style.zIndex = '2147483645';
          const badge = document.createElement('div');
          badge.dataset.role = 'badge';
          badge.textContent = message || 'INCIDENT DETECTED';
          badge.style.position = 'fixed';
          badge.style.top = '10px';
          badge.style.left = '12px';
          badge.style.padding = '4px 8px';
          badge.style.borderRadius = '999px';
          badge.style.font = '11px/1.2 monospace';
          badge.style.color = '#fff';
          badge.style.background = 'rgba(255,82,82,0.92)';
          badge.style.pointerEvents = 'none';
          wrap.appendChild(badge);
          document.documentElement.appendChild(wrap);
        };
        window.__bridgeSendSessionEvent = (event) => {
          const bar = document.getElementById('__bridge_session_top_bar');
          const stateRaw = bar?.dataset?.state || '{}';
          let state;
          try { state = JSON.parse(stateRaw); } catch (_e) { state = {}; }
          const controlUrl = window.__bridgeResolveControlUrl(state);
          if (!controlUrl) return;
          const payload = {
            ...(event || {}),
            session_id: state.session_id || '',
            url: String((event && event.url) || location.href || ''),
            controlled: !!state.controlled,
            learning_active: !!state.learning_active,
            observer_noise_mode: String(state.observer_noise_mode || 'minimal'),
          };
          fetch(`${controlUrl}/event`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            keepalive: true,
          }).catch(() => null);
        };
        window.__bridgeEnsureSessionObserver = () => {
          if (window.__bridgeObserverInstalled) return;
          window.__bridgeObserverInstalled = true;
          let lastMoveTs = 0;
          let lastMoveX = 0;
          let lastMoveY = 0;
          let lastScrollTs = 0;
          let lastScrollY = 0;
          const shouldCapture = (eventType, bridgeControl = false) => {
            const bar = document.getElementById('__bridge_session_top_bar');
            const stateRaw = bar?.dataset?.state || '{}';
            let state = {};
            try { state = JSON.parse(stateRaw); } catch (_e) { state = {}; }
            const mode = String(state.observer_noise_mode || 'minimal').toLowerCase();
            if (mode === 'debug') return true;
            const controlled = !!state.controlled;
            const learningActive = !!state.learning_active;
            if (eventType === 'click') {
              if (bridgeControl) return false;
              return controlled || learningActive;
            }
            if (eventType === 'scroll') {
              return learningActive;
            }
            if (eventType === 'mousemove') {
              return false;
            }
            return true;
          };
          const cssPath = (node) => {
            try {
              if (!node || !(node instanceof Element)) return '';
              if (node.id) return `#${node.id}`;
              const testid = node.getAttribute && (node.getAttribute('data-testid') || node.getAttribute('data-test'));
              if (testid) return `[data-testid="${testid}"]`;
              const tag = String(node.tagName || '').toLowerCase();
              const cls = String(node.className || '').trim().split(/\\s+/).filter(Boolean).slice(0, 2).join('.');
              if (tag) return cls ? `${tag}.${cls}` : tag;
              return '';
            } catch (_e) { return ''; }
          };
          document.addEventListener('click', (ev) => {
            const el = ev.target;
            let target = '';
            let selector = '';
            let text = '';
            let bridgeControl = false;
            let controlled = false;
            try {
              const bar = document.getElementById('__bridge_session_top_bar');
              const raw = bar?.dataset?.state || '{}';
              const state = JSON.parse(raw);
              controlled = !!state.controlled;
            } catch (_e) { controlled = false; }
            if (el && typeof el.closest === 'function') {
              const btn = el.closest('button,[role="button"],a,input,select,textarea');
              if (btn) {
                target = (btn.textContent || btn.id || btn.className || '').trim();
                selector = cssPath(btn);
                text = String(btn.textContent || '').trim().slice(0, 180);
                const bid = String(btn.id || '');
                bridgeControl = bid.startsWith('__bridge_') || selector.includes('__bridge_');
              }
            }
            if (!bridgeControl && !controlled && shouldCapture('click', bridgeControl)) {
              window.__bridgeShowClick?.(
                Number(ev.clientX || 0),
                Number(ev.clientY || 0),
                'manual click captured'
              );
            }
            if (!shouldCapture('click', bridgeControl)) return;
            window.__bridgeSendSessionEvent({
              type: 'click',
              target,
              selector,
              text,
              message: `click ${target}`,
              x: Number(ev.clientX || 0),
              y: Number(ev.clientY || 0),
            });
          }, true);
          window.addEventListener('mousemove', (ev) => {
            if (!shouldCapture('mousemove', false)) return;
            const now = Date.now();
            if ((now - lastMoveTs) < 350) return;
            const x = Number(ev.clientX || 0);
            const y = Number(ev.clientY || 0);
            const dist = Math.hypot(x - lastMoveX, y - lastMoveY);
            if (dist < 18) return;
            lastMoveTs = now;
            lastMoveX = x;
            lastMoveY = y;
            window.__bridgeSendSessionEvent({
              type: 'mousemove',
              message: `mousemove ${x},${y}`,
              x,
              y,
            });
          }, true);
          window.addEventListener('scroll', () => {
            if (!shouldCapture('scroll', false)) return;
            const now = Date.now();
            if ((now - lastScrollTs) < 300) return;
            const sy = Number(window.scrollY || window.pageYOffset || 0);
            const delta = Math.abs(sy - lastScrollY);
            if (delta < 80) return;
            lastScrollTs = now;
            lastScrollY = sy;
            window.__bridgeSendSessionEvent({
              type: 'scroll',
              message: `scroll y=${sy}`,
              scroll_y: sy,
            });
          }, { passive: true, capture: true });
          window.addEventListener('error', (ev) => {
            window.__bridgeSendSessionEvent({
              type: 'page_error',
              message: String(ev.message || 'window error'),
            });
          });
          window.addEventListener('unhandledrejection', (ev) => {
            window.__bridgeSendSessionEvent({
              type: 'page_error',
              message: String(ev.reason || 'unhandled rejection'),
            });
          });
          if (!window.__bridgeFetchWrapped && typeof window.fetch === 'function') {
            window.__bridgeFetchWrapped = true;
            const origFetch = window.fetch.bind(window);
            window.fetch = async (...args) => {
              try {
                const resp = await origFetch(...args);
                if (resp && Number(resp.status || 0) >= 400) {
                  window.__bridgeSendSessionEvent({
                    type: Number(resp.status || 0) >= 500 ? 'network_error' : 'network_warn',
                    status: Number(resp.status || 0),
                    url: String(resp.url || args[0] || ''),
                    message: `http ${resp.status}`,
                  });
                }
                return resp;
              } catch (err) {
                window.__bridgeSendSessionEvent({
                  type: 'network_error',
                  status: 0,
                  url: String(args[0] || ''),
                  message: String(err || 'fetch failed'),
                });
                throw err;
              }
            };
          }
          if (!window.__bridgeXhrWrapped && window.XMLHttpRequest) {
            window.__bridgeXhrWrapped = true;
            const origOpen = XMLHttpRequest.prototype.open;
            const origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method, url, ...rest) {
              this.__bridgeMethod = String(method || 'GET');
              this.__bridgeUrl = String(url || '');
              return origOpen.call(this, method, url, ...rest);
            };
            XMLHttpRequest.prototype.send = function(...args) {
              this.addEventListener('loadend', () => {
                const st = Number(this.status || 0);
                if (st >= 400 || st === 0) {
                  window.__bridgeSendSessionEvent({
                    type: (st === 0 || st >= 500) ? 'network_error' : 'network_warn',
                    status: st,
                    url: String(this.responseURL || this.__bridgeUrl || ''),
                    message: `xhr ${st}`,
                  });
                }
              });
              return origSend.apply(this, args);
            };
          }
        };
        window.__bridgeStartTopBarPolling = (state) => {
          const controlUrl = window.__bridgeResolveControlUrl(state || {});
          if (window.__bridgeTopBarPollTimer) {
            clearInterval(window.__bridgeTopBarPollTimer);
            window.__bridgeTopBarPollTimer = null;
          }
          if (!controlUrl) return;
          window.__bridgeTopBarPollTimer = setInterval(async () => {
            try {
              const resp = await fetch(`${controlUrl}/state`, { cache: 'no-store' });
              const payload = await resp.json();
              if (resp.ok && payload && typeof payload === 'object') {
                window.__bridgeUpdateTopBarState(payload);
              }
            } catch (_err) {
              // keep previous state; button actions will surface offline errors.
            }
          }, 2500);
        };
        window.__bridgeControlRequest = async (action) => {
          const bar = document.getElementById('__bridge_session_top_bar');
          const stateRaw = bar?.dataset?.state || '{}';
          let state;
          try { state = JSON.parse(stateRaw); } catch (_e) { state = {}; }
          const controlUrl = window.__bridgeResolveControlUrl(state);
          if (!controlUrl) {
            return { ok: false, error: 'agent offline' };
          }
          try {
            const resp = await fetch(`${controlUrl}/action`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ action }),
            });
            let payload = {};
            try { payload = await resp.json(); } catch (_e) { payload = {}; }
            if (!resp.ok) {
              const msg = payload.error || `http ${resp.status}`;
              return { ok: false, error: String(msg), payload };
            }
            return { ok: true, payload };
          } catch (err) {
            return { ok: false, error: String(err || 'agent offline') };
          }
        };
        window.__bridgeEnsureTopBar = (state) => {
          const id = '__bridge_session_top_bar';
          let bar = document.getElementById(id);
          if (!bar) {
            bar = document.createElement('div');
            bar.id = id;
            bar.style.position = 'fixed';
            bar.style.top = '0';
            bar.style.left = '0';
            bar.style.right = '0';
            bar.style.height = '42px';
            bar.style.display = 'flex';
            bar.style.alignItems = 'center';
            bar.style.gap = '10px';
            bar.style.padding = '6px 10px';
            bar.style.font = '12px/1.2 monospace';
            bar.style.zIndex = '2147483644';
            bar.style.pointerEvents = 'auto';
            bar.style.backdropFilter = 'blur(4px)';
            bar.style.borderBottom = '1px solid rgba(255,255,255,0.18)';
            bar.style.transform = 'translateY(-110%)';
            bar.style.opacity = '0';
            bar.style.transition = 'transform 210ms ease-out, opacity 210ms ease-out';
            bar.dataset.visible = '0';
            const hot = document.createElement('div');
            hot.id = '__bridge_top_hot';
            hot.style.position = 'fixed';
            hot.style.top = '0';
            hot.style.left = '0';
            hot.style.right = '0';
            hot.style.height = '8px';
            hot.style.pointerEvents = 'auto';
            hot.style.zIndex = '2147483643';
            hot.addEventListener('mouseenter', () => window.__bridgeSetTopBarVisible(true));
            bar.addEventListener('mouseleave', () => window.__bridgeSetTopBarVisible(false));
            const toggle = document.createElement('button');
            toggle.id = '__bridge_top_toggle';
            toggle.textContent = '◉';
            toggle.style.position = 'fixed';
            toggle.style.top = '6px';
            toggle.style.left = '6px';
            toggle.style.zIndex = '2147483644';
            toggle.style.width = '18px';
            toggle.style.height = '18px';
            toggle.style.padding = '0';
            toggle.style.font = '12px monospace';
            toggle.style.borderRadius = '999px';
            toggle.style.border = '1px solid rgba(255,255,255,0.35)';
            toggle.style.background = 'rgba(17,17,17,0.65)';
            toggle.style.color = '#fff';
            toggle.style.pointerEvents = 'auto';
            toggle.addEventListener('click', () => {
              window.__bridgeSetTopBarVisible(bar.dataset.visible !== '1');
            });
            overlayHost.appendChild(hot);
            overlayHost.appendChild(toggle);
            overlayHost.appendChild(bar);
          }
          window.__bridgeUpdateTopBarState(state);
        };
        window.__bridgeUpdateTopBarState = (state) => {
          const bar = document.getElementById('__bridge_session_top_bar');
          if (!bar) return;
          const s = state || {};
          const controlled = !!s.controlled;
          const open = String(s.state || 'open') === 'open';
          const controlUrl = window.__bridgeResolveControlUrl(s);
          const agentOnline = !!controlUrl && s.agent_online !== false;
          const incidentOpen = !!s.incident_open;
          const learningActive = !!s.learning_active;
          const readyManual = open && !controlled && agentOnline && !incidentOpen && !learningActive;
          const incidentText = String(s.last_error || '').slice(0, 96);
          bar.style.background = controlled
            ? 'rgba(59,167,255,0.22)'
            : (
              incidentOpen
                ? 'rgba(255,82,82,0.26)'
                : (
                  learningActive
                    ? 'rgba(245,158,11,0.24)'
                    : (
                      readyManual
                    ? 'rgba(22,163,74,0.22)'
                    : (open ? 'rgba(80,80,80,0.28)' : 'rgba(20,20,20,0.7)')
                    )
                )
            );
          bar.style.borderBottom = learningActive
            ? '2px solid rgba(245,158,11,0.95)'
            : (
              readyManual
                ? '2px solid rgba(34,197,94,0.95)'
                : '1px solid rgba(255,255,255,0.18)'
            );
          bar.dataset.state = JSON.stringify(s);
          window.__bridgeSetIncidentOverlay(incidentOpen && !controlled, incidentText || 'INCIDENT DETECTED');
          window.__bridgeSetStateBorder?.(s);
          window.__bridgeEnsureSessionObserver();
          window.__bridgeStartTopBarPolling(s);
          const ctrl = controlled
            ? 'ASSISTANT CONTROL'
            : (learningActive ? 'LEARNING/HANDOFF' : 'USER CONTROL');
          const url = String(s.url || '').slice(0, 70);
          const last = String(s.last_seen_at || '').replace('T', ' ').slice(0, 16);
          const status = !agentOnline
            ? 'agent offline'
            : (
              incidentOpen
                ? `incident open (${Number(s.error_count || 0)})`
                : ''
            );
          const readyBadge = readyManual
            ? `<span
                 id=\"__bridge_ready_badge\"
                 aria-label=\"session-ready-manual-test\"
                 style=\"
                   display:inline-flex;
                   align-items:center;
                   gap:6px;
                   background:#16a34a;
                   color:#fff;
                   border:1px solid #22c55e;
                   font-size:13px;
                   font-weight:700;
                   padding:6px 10px;
                   border-radius:999px;\"
               >● READY FOR MANUAL TEST</span>`
            : '';
          bar.innerHTML = `
            <strong>session ${s.session_id || '-'}</strong>
            <span>state:${s.state || '-'}</span>
            <span>control:${ctrl}</span>
            <span>url:${url}</span>
            <span>seen:${last}</span>
            ${readyBadge}
            <span id=\"__bridge_status_msg\" style=\"color:${agentOnline ? '#b7d8ff' : '#ffb3b3'}\">${status}</span>
            <button
              id=\"__bridge_ack_btn\" ${(open && agentOnline && incidentOpen) ? '' : 'disabled'}
            >Clear incident</button>
            <button id=\"__bridge_release_btn\" ${(open && agentOnline) ? '' : 'disabled'}>Release</button>
            <button id=\"__bridge_close_btn\" ${(open && agentOnline) ? '' : 'disabled'}>Close</button>
            <button id=\"__bridge_refresh_btn\" ${agentOnline ? '' : 'disabled'}>Refresh</button>
          `;
          const statusEl = bar.querySelector('#__bridge_status_msg');
          const ackBtn = bar.querySelector('#__bridge_ack_btn');
          const release = bar.querySelector('#__bridge_release_btn');
          const closeBtn = bar.querySelector('#__bridge_close_btn');
          const refresh = bar.querySelector('#__bridge_refresh_btn');
          const wire = (btn, action) => {
            if (!btn) return;
            btn.onclick = async () => {
              btn.disabled = true;
              if (statusEl) statusEl.textContent = `${action}...`;
              const result = await window.__bridgeControlRequest(action);
              if (!result.ok) {
                if (statusEl) statusEl.textContent = result.error || 'action failed';
                window.__bridgeUpdateTopBarState({ ...s, agent_online: false });
                return;
              }
              if (statusEl) statusEl.textContent = 'ok';
              window.__bridgeUpdateTopBarState(result.payload || s);
            };
          };
          wire(ackBtn, 'ack');
          wire(release, 'release');
          wire(closeBtn, 'close');
          wire(refresh, 'refresh');
        };
        window.__bridgeDestroyTopBar = () => {
          document.getElementById('__bridge_session_top_bar')?.remove();
          document.getElementById('__bridge_top_hot')?.remove();
          document.getElementById('__bridge_top_toggle')?.remove();
          window.__bridgeSetIncidentOverlay(false);
          if (window.__bridgeTopBarPollTimer) {
            clearInterval(window.__bridgeTopBarPollTimer);
            window.__bridgeTopBarPollTimer = null;
          }
        };
        if (sessionState && sessionState.session_id) {
          window.__bridgeEnsureTopBar(sessionState);
        }
        window.__bridgeOverlayInstalled = true;
        return true;
      };

      window.__bridgeEnsureOverlay = () => installOverlay();
      installOverlay();
    })();
    """
    script = script_template.replace("__CFG_JSON__", json.dumps(config, ensure_ascii=False))
    script = script.replace("__SESSION_JSON__", session_json)
    page.add_init_script(script)
    # Also execute on current page for attach/reuse flows where no navigation occurs.
    try:
        page.evaluate(script)
    except Exception:
        pass


def _highlight_target(
    page: Any,
    locator: Any,
    label: str,
    *,
    click_pulse_enabled: bool,
    show_preview: bool = True,
    auto_scroll: bool = True,
) -> tuple[float, float] | None:
    last_exc: Exception | None = None
    for _ in range(4):
        try:
            if auto_scroll:
                try:
                    locator.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    locator.evaluate("el => el.scrollIntoView({block:'center', inline:'center'})")
                except Exception:
                    pass

            info = locator.evaluate(
                """
                (el) => {
                  const r = el.getBoundingClientRect();
                  const x = r.left + (r.width / 2);
                  const y = r.top + (r.height / 2);
                  const inViewport = (
                    x >= 0 && y >= 0 &&
                    x <= window.innerWidth && y <= window.innerHeight &&
                    r.width > 0 && r.height > 0
                  );
                  const top = inViewport ? document.elementFromPoint(x, y) : null;
                  const ok = !!top && (top === el || (el.contains && el.contains(top)));
                  return { x, y, ok };
                }
                """
            )
            if isinstance(info, dict) and bool(info.get("ok", False)):
                x = float(info.get("x", 0.0))
                y = float(info.get("y", 0.0))
                if show_preview:
                    page.evaluate(
                        "([x, y, label]) => window.__bridgeShowClick?.(x, y, label)",
                        [x, y, label],
                    )
                if show_preview and click_pulse_enabled:
                    page.evaluate("([x, y]) => window.__bridgePulseAt?.(x, y)", [x, y])
                page.wait_for_timeout(120)
                return (x, y)

            if auto_scroll:
                # Likely occluded by fixed UI (e.g., dock). Scroll up a bit and retry.
                try:
                    page.evaluate("() => window.scrollBy(0, -120)")
                except Exception:
                    pass
                try:
                    page.wait_for_timeout(60)
                except Exception:
                    pass
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc is not None:
        return None
    return None


def _ensure_visual_overlay_installed(page: Any) -> None:
    try:
        page.evaluate("() => window.__bridgeEnsureOverlay?.()")
    except Exception:
        return


def _verify_visual_overlay_visible(page: Any) -> None:
    snapshot = _read_visual_overlay_snapshot(page)
    try:
        opacity = float(str(snapshot.get("opacity", "0") or "0"))
    except Exception:
        opacity = 0.0
    z_index = int(snapshot.get("z_index", 0) or 0)
    ok = bool(
        snapshot.get("exists")
        and snapshot.get("parent") == "body"
        and snapshot.get("display") != "none"
        and snapshot.get("visibility") != "hidden"
        and opacity > 0
        and z_index >= 2147483647
        and snapshot.get("pointer_events") == "none"
    )
    if not ok:
        raise RuntimeError(
            "Visual overlay not visible: missing #__bridge_cursor_overlay or invalid style."
        )


def _read_visual_overlay_snapshot(page: Any) -> dict[str, Any]:
    try:
        raw = page.evaluate(
            """
            () => {
              const el = document.getElementById('__bridge_cursor_overlay');
              if (!el) return { exists: false };
              const style = window.getComputedStyle(el);
              const parent = el.parentElement && el.parentElement.tagName
                ? el.parentElement.tagName.toLowerCase()
                : '';
              const z = Number.parseInt(style.zIndex || '0', 10);
              return {
                exists: true,
                parent,
                display: style.display || '',
                visibility: style.visibility || '',
                opacity: style.opacity || '0',
                z_index: Number.isNaN(z) ? 0 : z,
                pointer_events: style.pointerEvents || '',
              };
            }
            """
        )
    except Exception as exc:
        return {"exists": False, "error": str(exc)}
    if isinstance(raw, dict):
        return raw
    return {"exists": False, "error": "overlay snapshot is not a dict"}


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


def _human_mouse_move(page: Any, x: float, y: float, *, speed: float) -> None:
    # Humanized path: elliptical sway + noisy lateral drift + clear deceleration near target.
    global _LAST_HUMAN_ROUTE
    try:
        viewport = page.evaluate("() => ({w: window.innerWidth || 0, h: window.innerHeight || 0})")
        vw = float((viewport or {}).get("w") or 0)
        vh = float((viewport or {}).get("h") or 0)
    except Exception:
        vw = 0.0
        vh = 0.0

    def _clamp(px: float, py: float) -> tuple[float, float]:
        if vw > 0:
            px = max(0.0, min(vw - 1.0, px))
        if vh > 0:
            py = max(0.0, min(vh - 1.0, py))
        return px, py

    target_x, target_y = _clamp(float(x), float(y))
    start_x, start_y = target_x, target_y
    has_cursor_pos = False
    try:
        pos = page.evaluate("() => window.__bridgeGetCursorPos?.() || null")
        if isinstance(pos, dict):
            sx = pos.get("x")
            sy = pos.get("y")
            if isinstance(sx, (int, float)) and isinstance(sy, (int, float)):
                start_x, start_y = _clamp(float(sx), float(sy))
                has_cursor_pos = True
    except Exception:
        pass
    if not has_cursor_pos and vw > 0 and vh > 0:
        start_x, start_y = _clamp(vw * 0.5, vh * 0.5)

    rng = random.Random(int(time.time_ns()) ^ int(target_x * 131) ^ int(target_y * 197))
    norm_speed = max(0.25, float(speed))
    dx = target_x - start_x
    dy = target_y - start_y
    dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
    if dist < 2.5:
        _LAST_HUMAN_ROUTE = [(float(start_x), float(start_y)), (float(target_x), float(target_y))]
        try:
            page.evaluate("([x, y]) => window.__bridgeMoveCursor?.(x, y)", [target_x, target_y])
        except Exception:
            pass
        return

    nx = dx / dist
    ny = dy / dist
    perp_x = -ny
    perp_y = nx
    base_amp = max(10.0, min(44.0, dist * rng.uniform(0.1, 0.22)))
    c1x, c1y = _clamp(
        start_x + dx * rng.uniform(0.2, 0.34) + perp_x * base_amp * rng.uniform(-0.9, 0.9),
        start_y + dy * rng.uniform(0.2, 0.34) + perp_y * base_amp * rng.uniform(-0.9, 0.9),
    )
    c2x, c2y = _clamp(
        start_x + dx * rng.uniform(0.58, 0.8) + perp_x * base_amp * rng.uniform(-0.85, 0.85),
        start_y + dy * rng.uniform(0.58, 0.8) + perp_y * base_amp * rng.uniform(-0.85, 0.85),
    )
    overshoot = max(1.5, min(8.0, dist * rng.uniform(0.02, 0.045)))
    ox, oy = _clamp(
        target_x + nx * overshoot + perp_x * rng.uniform(-3.2, 3.2),
        target_y + ny * overshoot + perp_y * rng.uniform(-3.2, 3.2),
    )

    samples = int(max(18, min(52, round((dist / 24.0) + (24.0 / norm_speed)))))
    phase = rng.uniform(0.0, math.pi * 2.0)
    ellipse_cycles = rng.uniform(0.8, 1.8)
    route: list[tuple[float, float]] = []
    for i in range(1, samples + 1):
        t = i / float(samples)
        one_t = 1.0 - t
        bx = (
            one_t * one_t * one_t * start_x
            + 3.0 * one_t * one_t * t * c1x
            + 3.0 * one_t * t * t * c2x
            + t * t * t * ox
        )
        by = (
            one_t * one_t * one_t * start_y
            + 3.0 * one_t * one_t * t * c1y
            + 3.0 * one_t * t * t * c2y
            + t * t * t * oy
        )
        # Elliptical lateral motion with tapering envelope near endpoints.
        env = max(0.0, math.sin(math.pi * t))
        wobble = math.sin((2.0 * math.pi * ellipse_cycles * t) + phase)
        bx += perp_x * (base_amp * 0.84 * env * wobble)
        by += perp_y * (base_amp * 0.84 * env * wobble)
        # Fine-grained noise, stronger at mid-path and softer near target.
        micro = max(0.0, 1.0 - abs(0.52 - t) * 1.85)
        bx += perp_x * rng.uniform(-2.8, 2.8) * micro
        by += perp_y * rng.uniform(-2.8, 2.8) * micro
        route.append(_clamp(bx, by))
    route.append(_clamp(target_x, target_y))
    _LAST_HUMAN_ROUTE = [(float(px), float(py)) for px, py in route]
    route_payload = [[float(px), float(py)] for px, py in route]
    try:
        page.evaluate(
            "pts => { window.__bridgeLastHumanRoute = pts; window.__bridgeDrawPath?.(pts); }",
            route_payload,
        )
    except Exception:
        pass

    last_pause_idx = len(route) - 2
    for idx, (px, py) in enumerate(route):
        if idx < len(route) - 1:
            progress = min(1.0, idx / max(1, len(route) - 1))
            # Decelerate near destination: fewer large jumps, finer final approach.
            slow_factor = progress * progress
            seg_steps = int(
                max(
                    2,
                    min(
                        11,
                        round((3.2 / norm_speed) + (slow_factor * 6.0) + rng.uniform(-0.8, 1.3)),
                    ),
                )
            )
        else:
            seg_steps = 3
        page.mouse.move(px, py, steps=seg_steps)
        try:
            # Feed intermediate points to the visual overlay so the trail reflects the real path.
            page.evaluate("([x, y]) => window.__bridgeMoveCursor?.(x, y)", [px, py])
        except Exception:
            pass
        if idx < last_pause_idx:
            progress = min(1.0, idx / max(1, len(route) - 1))
            # Tiny cadence pauses; slightly longer near end to make slowdown perceptible.
            pause_ms = int(
                max(
                    0,
                    min(
                        18,
                        round((3.2 / norm_speed) + (progress * 5.0) + rng.uniform(-2.0, 2.4)),
                    ),
                )
            )
            if pause_ms > 0:
                try:
                    page.wait_for_timeout(pause_ms)
                except Exception:
                    pass
    try:
        page.evaluate("([x, y]) => window.__bridgeMoveCursor?.(x, y)", [target_x, target_y])
    except Exception:
        pass


def _human_mouse_click(page: Any, x: float, y: float, *, speed: float, hold_ms: int) -> None:
    _human_mouse_move(page, x, y, speed=speed)
    # Tiny jitter right before click to avoid perfectly static pre-click posture.
    try:
        jitter_x = x + random.uniform(-1.5, 1.5)
        jitter_y = y + random.uniform(-1.5, 1.5)
        page.mouse.move(jitter_x, jitter_y, steps=2)
        page.mouse.move(x, y, steps=2)
    except Exception:
        pass
    try:
        page.evaluate("([x, y]) => window.__bridgePulseAt?.(x, y)", [x, y])
    except Exception:
        pass
    page.mouse.down()
    effective_hold = int(max(0, min(260, round(float(hold_ms) * 0.34))))
    if effective_hold > 0:
        page.wait_for_timeout(effective_hold)
    page.mouse.up()
    # Post-click settle: small drift so cursor doesn't remain pinned on exact click coordinate.
    try:
        settle_x = x + random.uniform(-16.0, 16.0)
        settle_y = y + random.uniform(-12.0, 12.0)
        page.mouse.move(settle_x, settle_y, steps=4)
        page.evaluate("([x, y]) => window.__bridgeMoveCursor?.(x, y)", [settle_x, settle_y])
    except Exception:
        pass


def _collapse_ws(text: str) -> str:
    return " ".join(str(text).replace("\n", " ").replace("\r", " ").split())


def _demo_login_button_available(page: Any) -> bool:
    try:
        btn = page.get_by_role("button", name="Entrar demo")
        if btn.count() <= 0:
            return False
        try:
            if not btn.first.is_visible(timeout=800):
                return False
        except Exception:
            return False
        try:
            return bool(btn.first.is_enabled())
        except Exception:
            return True
    except Exception:
        return False


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


def _set_assistant_control_overlay(page: Any, enabled: bool) -> None:
    if _page_is_closed(page):
        return
    try:
        page.evaluate(
            """
        ([enabled]) => {
          const id = '__bridge_assistant_control_overlay';
          const existing = document.getElementById(id);
          if (!enabled) {
            if (existing) existing.remove();
            return;
          }
          if (existing) return;
          const wrap = document.createElement('div');
          wrap.id = id;
          wrap.style.position = 'fixed';
          wrap.style.inset = '0';
          wrap.style.border = '3px solid #3BA7FF';
          wrap.style.boxSizing = 'border-box';
          wrap.style.pointerEvents = 'none';
          wrap.style.zIndex = '2147483645';
          const badge = document.createElement('div');
          badge.textContent = 'ASSISTANT CONTROL';
          badge.style.position = 'fixed';
          badge.style.top = '10px';
          badge.style.right = '12px';
          badge.style.padding = '4px 8px';
          badge.style.borderRadius = '999px';
          badge.style.font = '11px/1.2 monospace';
          badge.style.color = '#fff';
          badge.style.background = 'rgba(59,167,255,0.9)';
          badge.style.pointerEvents = 'none';
          wrap.appendChild(badge);
          document.documentElement.appendChild(wrap);
        }
            """,
            [enabled],
        )
    except Exception:
        return


def _set_user_control_overlay(page: Any, enabled: bool) -> None:
    if _page_is_closed(page):
        return
    try:
        page.evaluate(
            """
        ([enabled]) => {
          const id = '__bridge_user_control_overlay';
          const existing = document.getElementById(id);
          if (!enabled) {
            if (existing) existing.remove();
            return;
          }
          if (existing) return;
          const wrap = document.createElement('div');
          wrap.id = id;
          wrap.style.position = 'fixed';
          wrap.style.inset = '0';
          wrap.style.border = '3px solid #22c55e';
          wrap.style.boxSizing = 'border-box';
          wrap.style.pointerEvents = 'none';
          wrap.style.zIndex = '2147483644';
          const badge = document.createElement('div');
          badge.textContent = 'USER CONTROL';
          badge.style.position = 'fixed';
          badge.style.top = '10px';
          badge.style.right = '12px';
          badge.style.padding = '4px 8px';
          badge.style.borderRadius = '999px';
          badge.style.font = '11px/1.2 monospace';
          badge.style.color = '#fff';
          badge.style.background = 'rgba(34,197,94,0.9)';
          badge.style.pointerEvents = 'none';
          wrap.appendChild(badge);
          document.documentElement.appendChild(wrap);
        }
            """,
            [enabled],
        )
    except Exception:
        return


def _set_learning_handoff_overlay(page: Any, enabled: bool) -> None:
    if _page_is_closed(page):
        return
    try:
        page.evaluate(
            """
        ([enabled]) => {
          const id = '__bridge_learning_handoff_overlay';
          const existing = document.getElementById(id);
          if (!enabled) {
            if (existing) existing.remove();
            return;
          }
          if (existing) return;
          const wrap = document.createElement('div');
          wrap.id = id;
          wrap.style.position = 'fixed';
          wrap.style.inset = '0';
          wrap.style.border = '3px solid #f59e0b';
          wrap.style.boxSizing = 'border-box';
          wrap.style.pointerEvents = 'none';
          wrap.style.zIndex = '2147483645';
          const badge = document.createElement('div');
          badge.textContent = 'LEARNING/HANDOFF';
          badge.style.position = 'fixed';
          badge.style.top = '10px';
          badge.style.right = '12px';
          badge.style.padding = '4px 8px';
          badge.style.borderRadius = '999px';
          badge.style.font = '11px/1.2 monospace';
          badge.style.color = '#111';
          badge.style.background = 'rgba(245,158,11,0.95)';
          badge.style.pointerEvents = 'none';
          wrap.appendChild(badge);
          document.documentElement.appendChild(wrap);
        }
            """,
            [enabled],
        )
    except Exception:
        return


def _session_state_payload(
    session: WebSession | None,
    *,
    override_controlled: bool | None = None,
    override_state: str | None = None,
    learning_active: bool = False,
) -> dict[str, Any]:
    if session is None:
        return {}
    control_port = int(session.control_port or 0)
    return {
        "session_id": session.session_id,
        "url": session.url,
        "title": session.title,
        "controlled": session.controlled if override_controlled is None else override_controlled,
        "state": session.state if override_state is None else override_state,
        "learning_active": bool(learning_active),
        "observer_noise_mode": _observer_noise_mode(),
        "last_seen_at": session.last_seen_at,
        "control_port": control_port,
        "control_url": f"http://127.0.0.1:{control_port}" if control_port > 0 else "",
        "agent_online": control_port > 0,
    }


def _update_top_bar_state(page: Any, payload: dict[str, Any]) -> None:
    if _page_is_closed(page):
        return
    try:
        page.evaluate("([payload]) => window.__bridgeUpdateTopBarState?.(payload)", [payload])
    except Exception:
        return


def _destroy_top_bar(page: Any) -> None:
    if _page_is_closed(page):
        return
    try:
        page.evaluate("() => window.__bridgeDestroyTopBar?.()")
    except Exception:
        return


def _notify_learning_state(session: WebSession | None, *, active: bool, window_seconds: int) -> None:
    if session is None:
        return
    port = int(session.control_port or 0)
    if port <= 0:
        return
    payload = {
        "type": "learning_on" if active else "learning_off",
        "window_seconds": int(max(1, min(600, window_seconds))),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/event",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0):
            return
    except Exception:
        return


def _same_origin_path(current_url: str, target_url: str) -> bool:
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


def _to_repo_rel(path: Path) -> str:
    return str(path.resolve().relative_to(Path.cwd()))


def _normalize_url(raw: str) -> str:
    return raw.rstrip(".,;:!?)]}\"'")


def _is_valid_url(text: str) -> bool:
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
