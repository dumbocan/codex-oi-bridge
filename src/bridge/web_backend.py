"""Deterministic web interaction backend using Playwright."""

from __future__ import annotations

import importlib.util
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
from bridge.storage import write_json, write_status
from bridge.web_session import (
    WebSession,
    mark_controlled,
    request_session_state,
)


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_CLICK_TEXT_RE = re.compile(
    r"(?:click|haz\s+click|pulsa|presiona)[^\"'<>]{0,120}[\"'“”]([^\"'“”]{1,120})[\"'“”]",
    flags=re.IGNORECASE,
)
_SELECTOR_RE = re.compile(
    r"selector\s*[=:]?\s*[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)
_CLICK_SELECTOR_RE = re.compile(
    r"(?:click|haz\s+click|pulsa|presiona)\s+(?:en\s+)?(?:el\s+)?"
    r"selector\s*[=:]?\s*[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)
_SELECT_LABEL_RE = re.compile(
    r"\b(?:select|selecciona)\b[^\n\r]{0,120}?"
    r"(?:label|texto|opci[oó]n|option)?\s*[=:]?\s*"
    r"[\"'“”]([^\"'“”]{1,120})[\"'“”][^\n\r]{0,120}?"
    r"(?:from|en)\s+(?:selector\s*[=:]?\s*)?"
    r"[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)
_SELECT_VALUE_RE = re.compile(
    r"\b(?:select|selecciona)\b[^\n\r]{0,80}?value\s*[=:]?\s*"
    r"[\"'“”]([^\"'“”]{1,120})[\"'“”][^\n\r]{0,80}?"
    r"(?:from|en)\s+(?:selector\s*[=:]?\s*)?"
    r"[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)
_WAIT_SELECTOR_RE = re.compile(
    r"(?:wait|espera)(?:\s+for)?\s+selector\s*[=:]?\s*[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)
_WAIT_TEXT_RE = re.compile(
    r"(?:wait|espera)(?:\s+for)?\s+text\s*[=:]?\s*[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)

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


@dataclass(frozen=True)
class WebStep:
    kind: str
    target: str
    value: str = ""


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
    captures: list[tuple[int, int, WebStep]] = []

    for match in _SELECT_VALUE_RE.finditer(task):
        captures.append(
            (
                match.start(),
                match.end(),
                WebStep("select_value", match.group(2).strip(), match.group(1).strip()),
            )
        )
    for match in _SELECT_LABEL_RE.finditer(task):
        captures.append(
            (
                match.start(),
                match.end(),
                WebStep("select_label", match.group(2).strip(), match.group(1).strip()),
            )
        )
    for match in _WAIT_SELECTOR_RE.finditer(task):
        captures.append((match.start(), match.end(), WebStep("wait_selector", match.group(1).strip())))
    for match in _WAIT_TEXT_RE.finditer(task):
        captures.append((match.start(), match.end(), WebStep("wait_text", match.group(1).strip())))
    for match in _CLICK_SELECTOR_RE.finditer(task):
        captures.append((match.start(), match.end(), WebStep("click_selector", match.group(1).strip())))

    if captures:
        captures.sort(key=lambda item: item[0])
        filtered: list[tuple[int, int, WebStep]] = []
        for start, end, step in captures:
            if any(start < prev_end and end > prev_start for prev_start, prev_end, _ in filtered):
                continue
            filtered.append((start, end, step))

        tail_texts = _text_clicks_outside_spans(task, [(start, end) for start, end, _ in filtered])
        for start, _end, text in tail_texts:
            filtered.append((start, start, WebStep("click_text", text)))
        filtered.sort(key=lambda item: item[0])
        return [step for _start, _end, step in filtered]

    steps: list[WebStep] = []
    for match in _WAIT_SELECTOR_RE.finditer(task):
        steps.append(WebStep("wait_selector", match.group(1).strip()))
    for match in _WAIT_TEXT_RE.finditer(task):
        steps.append(WebStep("wait_text", match.group(1).strip()))
    for match in _SELECT_LABEL_RE.finditer(task):
        steps.append(WebStep("select_label", match.group(2).strip(), match.group(1).strip()))
    for match in _SELECT_VALUE_RE.finditer(task):
        steps.append(WebStep("select_value", match.group(2).strip(), match.group(1).strip()))
    for match in _SELECTOR_RE.finditer(task):
        steps.append(WebStep("click_selector", match.group(1).strip()))
    for match in _CLICK_TEXT_RE.finditer(task):
        steps.append(WebStep("click_text", match.group(1).strip()))
    return steps


def _text_clicks_outside_spans(task: str, spans: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for match in _CLICK_TEXT_RE.finditer(task):
        if any(match.start() < end and match.end() > start for start, end in spans):
            continue
        found.append((match.start(), match.end(), match.group(1).strip()))
    return found


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
    current_step_signature = ""
    current_learning_target = ""
    last_step_change_ts = time.monotonic()
    last_progress_event_ts = last_step_change_ts
    last_useful_events = 0
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
        stuck_interactive_seconds = float(os.getenv("BRIDGE_WEB_STUCK_INTERACTIVE_SECONDS", "12"))
        stuck_step_seconds = float(os.getenv("BRIDGE_WEB_STUCK_STEP_SECONDS", "20"))
        stuck_iframe_seconds = float(os.getenv("BRIDGE_WEB_STUCK_IFRAME_SECONDS", "8"))
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
        last_useful_events = _observer_useful_event_count(session)
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

            def _poll_watchdog_progress() -> None:
                nonlocal last_useful_events, last_progress_event_ts
                useful = _observer_useful_event_count(session)
                if useful > last_useful_events:
                    last_useful_events = useful
                    last_progress_event_ts = time.monotonic()

            def _watchdog_stuck_attempt(attempted: str) -> bool:
                nonlocal handoff_reason, handoff_where, handoff_attempted
                nonlocal force_keep_open, wait_for_human_learning, failed_target_for_teaching
                nonlocal control_enabled, result, release_for_handoff
                _poll_watchdog_progress()
                now = time.monotonic()
                if (
                    current_step_signature
                    and (now - last_progress_event_ts) > max(0.1, stuck_iframe_seconds)
                    and _is_iframe_focus_locked(page)
                ):
                    handoff_reason = "stuck_iframe_focus"
                    handoff_where = current_step_signature
                    handoff_attempted = f"{attempted}, iframe_focus>{stuck_iframe_seconds}s"
                    failed_target_for_teaching = current_learning_target
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
                if (
                    current_step_signature
                    and (now - last_step_change_ts) > max(0.1, stuck_step_seconds)
                ) or (
                    current_step_signature
                    and (now - last_progress_event_ts) > max(0.1, stuck_interactive_seconds)
                ):
                    handoff_reason = "stuck"
                    handoff_where = current_step_signature
                    handoff_attempted = attempted
                    failed_target_for_teaching = current_learning_target
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

            def _remaining_ms(deadline_ts: float) -> int:
                return int(max(0.0, deadline_ts - time.monotonic()) * 1000)

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
                if step_sig != current_step_signature:
                    current_step_signature = step_sig
                    last_step_change_ts = time.monotonic()
                    last_progress_event_ts = last_step_change_ts
                current_learning_target = (
                    str(step.target).strip()
                    if step.kind in {"click_selector", "click_text", "maybe_click_text", "select_label", "select_value"}
                    else ""
                )
                if _runtime_closed(page, session):
                    result = "failed"
                    ui_findings.append("what_failed=run_crash")
                    ui_findings.append("where=web-run")
                    ui_findings.append("why_likely=page_or_context_closed")
                    ui_findings.append("attempted=executor run")
                    ui_findings.append("next_best_action=reopen session and retry")
                    break
                if time.monotonic() > run_deadline_ts:
                    if _trigger_timeout_handoff(
                        what_failed="run_timeout",
                        where=current_step_signature or "web-run",
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

                if step.kind in (
                    "click_selector",
                    "click_text",
                    "maybe_click_text",
                    "select_label",
                    "select_value",
                ):
                    step_started_at = time.monotonic()
                    step_deadline_ts = step_started_at + step_hard_timeout_seconds
                    if min(_remaining_ms(step_deadline_ts), _remaining_ms(run_deadline_ts)) <= 0:
                        if _trigger_timeout_handoff(
                            what_failed="interactive_timeout",
                            where=current_step_signature or f"step {idx}/{total}",
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
                            handoff_where = current_step_signature
                            handoff_attempted = "main-frame-first precheck failed"
                            failed_target_for_teaching = step.target
                            release_for_handoff = True
                            _show_custom_handoff_notice(
                                page, "Me he quedado dentro de YouTube iframe. Te cedo el control."
                            )
                            ui_findings.append("Me he quedado dentro de YouTube iframe. Te cedo el control.")
                            ui_findings.append("what_failed=stuck_iframe_focus")
                            ui_findings.append(f"where={handoff_where}")
                            ui_findings.append(
                                "why_likely=unable to return focus/context to main frame before interactive action"
                            )
                            ui_findings.append(f"attempted={handoff_attempted}")
                            ui_findings.append("next_best_action=human_assist")
                            result = "partial"
                            break
                        raise RuntimeError("Unable to return to main frame context before interactive step")
                    interactive_step += 1
                    before = evidence_dir / f"step_{interactive_step}_before.png"
                    after = evidence_dir / f"step_{interactive_step}_after.png"
                    page.screenshot(path=str(before), full_page=True)
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
                                where=current_step_signature or f"step {idx}/{total}",
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
                                    where=current_step_signature or f"step {idx}/{total}",
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
                                handoff_where = current_step_signature
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
                            )
                    except Exception as exc:
                        if _is_page_closed_error(exc) or _runtime_closed(page, session):
                            result = "failed"
                            ui_findings.append("what_failed=run_crash")
                            ui_findings.append("where=web-run")
                            ui_findings.append("why_likely=page_or_context_closed")
                            ui_findings.append("attempted=executor run")
                            ui_findings.append("next_best_action=reopen session and retry")
                            break
                        if teaching_mode and step.kind in ("click_text", "click_selector"):
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
                                page.screenshot(path=str(timeout_path), full_page=True)
                                evidence_paths.append(_to_repo_rel(timeout_path))
                            except Exception:
                                pass
                            console_errors.append(
                                f"Timeout on interactive step {interactive_step}: {step.kind} {step.target}"
                            )
                            ui_findings.append(
                                f"step {interactive_step} timeout on {step.kind}:{step.target} "
                                f"(timeout_ms={interactive_timeout_ms})"
                            )
                            ui_findings.append("what_failed=interactive_timeout")
                            ui_findings.append(f"where=step {interactive_step}:{step.kind}:{step.target}")
                            ui_findings.append(
                                "why_likely=target unavailable/occluded or app did not become interactive in time"
                            )
                            ui_findings.append("attempted=interactive timeout path")
                            ui_findings.append("next_best_action=inspect target visibility or use teaching handoff")
                            result = "failed"
                            break
                        raise
                    if handoff_reason in {"stuck", "stuck_iframe_focus"}:
                        break
                    if len(actions) > prev_action_len:
                        last_progress_event_ts = time.monotonic()
                    page.wait_for_timeout(1000)
                    page.screenshot(path=str(after), full_page=True)
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
                            where=current_step_signature or f"step {idx}/{total}",
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
                            handoff_where = current_step_signature
                            handoff_attempted = "main-frame-first precheck failed"
                            failed_target_for_teaching = step.target
                            release_for_handoff = True
                            _show_custom_handoff_notice(
                                page, "Me he quedado dentro de YouTube iframe. Te cedo el control."
                            )
                            ui_findings.append("Me he quedado dentro de YouTube iframe. Te cedo el control.")
                            ui_findings.append("what_failed=stuck_iframe_focus")
                            ui_findings.append(f"where={handoff_where}")
                            ui_findings.append(
                                "why_likely=unable to return focus/context to main frame before wait step"
                            )
                            ui_findings.append(f"attempted={handoff_attempted}")
                            ui_findings.append("next_best_action=human_assist")
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
                        ui_findings.append("what_failed=run_crash")
                        ui_findings.append("where=web-run")
                        ui_findings.append("why_likely=page_or_context_closed")
                        ui_findings.append("attempted=executor run")
                        ui_findings.append("next_best_action=reopen session and retry")
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
                            page.screenshot(path=str(timeout_path), full_page=True)
                            evidence_paths.append(_to_repo_rel(timeout_path))
                        except Exception:
                            pass
                        console_errors.append(f"Timeout on step {idx}: {step.kind} {step.target}")
                        ui_findings.append(
                            f"step {idx} timeout waiting for {step.kind}:{step.target} (timeout_ms={wait_timeout_ms})"
                        )
                        ui_findings.append("what_failed=wait_timeout")
                        ui_findings.append(f"where=step {idx}:{step.kind}:{step.target}")
                        ui_findings.append(
                            "why_likely=expected selector/text did not appear within timeout window"
                        )
                        ui_findings.append("attempted=wait timeout path")
                        ui_findings.append("next_best_action=verify app state or retry with stable selector")
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
                last_progress_event_ts = time.monotonic()
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
    if force_keep_open:
        ui_findings.append("teaching handoff: browser kept open for manual control")
    if result != "failed" and (console_errors or network_findings):
        result = "partial"
    if verified and steps and not ui_findings:
        result = "failed"
        ui_findings.append("what_failed=verified_mode_missing_findings")
        ui_findings.append("where=post-run")
        ui_findings.append("why_likely=verified mode requires explicit visible verification findings")
        ui_findings.append("attempted=verified post-check")
        ui_findings.append("next_best_action=add verify visible result findings")
    _ensure_structured_ui_findings(
        ui_findings,
        result=result,
        where_default=current_step_signature or "web-run",
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
    keys = ("what_failed=", "where=", "why_likely=", "attempted=", "next_best_action=")
    has = {k: any(str(item).startswith(k) for item in ui_findings) for k in keys}
    if result == "success":
        defaults = {
            "what_failed=": "none",
            "where=": "n/a",
            "why_likely=": "n/a",
            "attempted=": "normal execution",
            "next_best_action=": "none",
        }
    else:
        defaults = {
            "what_failed=": "unknown",
            "where=": where_default or "web-run",
            "why_likely=": "run ended without explicit failure classification",
            "attempted=": "executor run",
            "next_best_action=": "inspect report/logs and retry",
        }
    for key in keys:
        if not has[key]:
            ui_findings.append(f"{key}{defaults[key]}")
    if not any(str(item).startswith("final_state=") for item in ui_findings):
        ui_findings.append(f"final_state={result}")


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
        for selector in _stable_selectors_for_target(step.target):
            candidates.append(WebStep("click_selector", selector))
        for selector in learning_selectors:
            candidates.insert(1, WebStep("click_selector", selector))
    elif step.kind == "click_selector":
        for selector in learning_selectors:
            candidates.insert(0, WebStep("click_selector", selector))
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
            page.screenshot(path=str(before_retry), full_page=True)
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
                )
                try:
                    page.screenshot(path=str(after_retry), full_page=True)
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
            page.screenshot(path=str(after_retry), full_page=True)
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


def _retry_scroll(page: Any) -> None:
    try:
        page.evaluate(
            """
            () => {
              const main = document.querySelector('main,[role="main"],#main,.main,#__next,.app,[data-testid="main"]');
              if (main && typeof main.scrollBy === 'function') {
                main.scrollBy(0, Math.max(280, Math.floor(window.innerHeight * 0.45)));
              }
              window.scrollBy(0, Math.max(320, Math.floor(window.innerHeight * 0.55)));
            }
            """
        )
    except Exception:
        try:
            page.evaluate("() => window.scrollBy(0, 320)")
        except Exception:
            pass
    try:
        page.wait_for_timeout(220)
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
    msg = f"No encuentro el botón: {target}. Te cedo el control."
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
    label = target or "ese control"
    msg = f"Gracias, ya he aprendido dónde está {label}. Ya continúo yo."
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
                el.style.background = 'rgba(16,185,129,0.96)';
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


def _normalize_failed_target_label(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    tail = text.split(":")[-1].strip().strip("'\"")
    return tail


def _show_wrong_manual_click_notice(page: Any, failed_target: str) -> None:
    label = _normalize_failed_target_label(failed_target) or "objetivo esperado"
    suggestion = _stable_selectors_for_target(label)
    hint = suggestion[0] if suggestion else label
    msg = f"Ese click no coincide. El objetivo es '{label}'. Prueba con: {hint}"
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
                el.style.background = 'rgba(239,68,68,0.96)';
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


def _capture_manual_learning(
    *,
    page: Any | None,
    session: WebSession,
    failed_target: str,
    context: dict[str, str],
    wait_seconds: int,
) -> dict[str, Any] | None:
    max_wait = max(4, min(180, int(wait_seconds)))
    deadline = datetime.now(timezone.utc).timestamp() + max_wait
    seen: set[str] = set()
    while datetime.now(timezone.utc).timestamp() < deadline:
        try:
            state = request_session_state(session)
        except BaseException:
            return None
        events = list(state.get("recent_events", []) or [])
        for evt in reversed(events):
            if not isinstance(evt, dict):
                continue
            key = "|".join(
                [
                    str(evt.get("created_at", "")),
                    str(evt.get("type", "")),
                    str(evt.get("message", "")),
                ]
            )
            if key in seen:
                continue
            seen.add(key)
            if str(evt.get("type", "")).strip().lower() != "click":
                continue
            if not _is_relevant_manual_learning_event(evt, failed_target):
                if page is not None:
                    _show_wrong_manual_click_notice(page, failed_target)
                continue
            selector = str(evt.get("selector", "")).strip()
            target = str(evt.get("target", "")).strip()
            return {
                "failed_target": failed_target or target,
                "selector": selector,
                "target": target,
                "timestamp": str(evt.get("created_at", "")),
                "url": str(evt.get("url", "")),
                "state_key": context.get("state_key", ""),
            }
        try:
            from time import sleep

            sleep(0.7)
        except Exception:
            break
    return None


def _is_relevant_manual_learning_event(evt: dict[str, Any], failed_target: str) -> bool:
    selector = str(evt.get("selector", "")).strip().lower()
    target = str(evt.get("target", "")).strip().lower()
    text = str(evt.get("text", "")).strip().lower()
    message = str(evt.get("message", "")).strip().lower()

    # Ignore bridge control widgets/buttons.
    if "__bridge_" in selector:
        return False
    if target in {"release", "close", "refresh", "clear incident", "ack"}:
        return False

    raw = str(failed_target or "").strip().lower()
    if not raw:
        return True
    probe = raw.split(":")[-1].strip().strip("'\"")
    if not probe:
        return True
    if probe.startswith("#") and probe in selector:
        return True
    token = re.sub(r"[^a-z0-9]+", " ", probe).strip()
    if not token:
        return True
    if token in selector or token in target or token in text or token in message:
        return True
    parts = [p for p in token.split() if len(p) >= 3]
    if parts and any(p in selector for p in parts) and ("stop" in parts or "play" in parts):
        return True
    return False


def _write_teaching_artifacts(run_dir: Path, payload: dict[str, Any]) -> list[str]:
    out_dir = run_dir / "learning"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"teaching_{stamp}"
    json_path = out_dir / f"{base}.json"
    md_path = out_dir / f"{base}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_lines = [
        "# Teaching Artifact",
        "",
        f"- failed_target: `{payload.get('failed_target', '')}`",
        f"- selector: `{payload.get('selector', '')}`",
        f"- click_target_text: `{payload.get('target', '')}`",
        f"- timestamp: `{payload.get('timestamp', '')}`",
        f"- url: `{payload.get('url', '')}`",
        f"- state_key: `{payload.get('state_key', '')}`",
    ]
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return [_to_repo_rel(json_path), _to_repo_rel(md_path)]


def _resume_after_learning(
    *,
    page: Any,
    selector: str,
    target: str,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
) -> bool:
    sel = str(selector or "").strip()
    if not sel:
        return False
    try:
        locator = page.locator(sel).first
        locator.wait_for(state="visible", timeout=3500)
        locator.click(timeout=3500)
        actions.append(f"cmd: playwright click selector:{sel} (learning-resume)")
        observations.append(f"learning-resume clicked selector: {sel}")
        ui_findings.append(f"learning_resume=success target={target}")
        return True
    except Exception:
        return False


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
) -> None:
    iframe_guard = _disable_active_youtube_iframe_pointer_events(page)
    if not _force_main_frame_context(page):
        _restore_iframe_pointer_events(page, iframe_guard)
        raise RuntimeError("Unable to enforce main frame context for interactive step")
    try:
        if step.kind == "click_selector":
            locator = page.locator(step.target).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            target = _highlight_target(
                page,
                locator,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled and visual,
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
                if str(step.target).strip().lower() == "reproducir":
                    fallback = page.locator('.track-card:has-text("Stan") button:has-text("Reproducir")').first
                    fallback.wait_for(state="visible", timeout=timeout_ms)
                    target = _highlight_target(
                        page,
                        fallback,
                        f"step {step_num} fallback",
                        click_pulse_enabled=click_pulse_enabled and visual,
                    )
                    if target is not None:
                        if visual and visual_human_mouse and target:
                            _human_mouse_click(
                                page,
                                target[0],
                                target[1],
                                speed=visual_mouse_speed,
                                hold_ms=visual_click_hold_ms,
                            )
                        else:
                            fallback.click(timeout=timeout_ms)
                        actions.append(
                            'cmd: playwright click selector:.track-card:has-text("Stan") '
                            'button:has-text("Reproducir")'
                        )
                        observations.append(
                            f"Clicked fallback selector in step {step_num}: .track-card:has-text('Stan') "
                            "button:has-text('Reproducir')"
                        )
                        ui_findings.append(
                            f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}"
                        )
                        return
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

        if step.kind == "select_label":
            locator = page.locator(step.target).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            target = _highlight_target(
                page,
                locator,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled and visual,
            )
            if target is None:
                raise SystemExit(f"Target occluded or not visible: selector {step.target}")
            if visual:
                if visual_human_mouse and target:
                    _human_mouse_move(page, target[0], target[1], speed=visual_mouse_speed)
            locator.select_option(label=step.value)
            actions.append(f"cmd: playwright select selector:{step.target} label:{step.value}")
            observations.append(
                f"Selected option by label in step {step_num}: selector={step.target}, label={step.value}"
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
            )
            if target is None:
                raise SystemExit(f"Target occluded or not visible: selector {step.target}")
            if visual:
                if visual_human_mouse and target:
                    _human_mouse_move(page, target[0], target[1], speed=visual_mouse_speed)
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
        if (window.__bridgeOverlayInstalled) return true;
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
        };

        const emitTrail = (x, y) => {
        if (!cfg.traceEnabled) return;
        const dot = document.createElement('div');
        dot.style.position = 'fixed';
        dot.style.left = `${Math.max(0, x - 3)}px`;
        dot.style.top = `${Math.max(0, y - 3)}px`;
        dot.style.width = '6px';
        dot.style.height = '6px';
        dot.style.borderRadius = '50%';
        dot.style.background = 'rgba(59,167,255,0.45)';
        dot.style.pointerEvents = 'none';
        dot.style.transition = 'opacity 380ms ease';
        trailLayer.appendChild(dot);
        requestAnimationFrame(() => { dot.style.opacity = '0'; });
        setTimeout(() => dot.remove(), 420);
        };

        const setCursor = (x, y) => {
        const normal = 14 * cfg.scale;
        cursor.style.width = `${normal}px`;
        cursor.style.height = `${normal}px`;
        cursor.style.left = `${Math.max(0, x - normal / 2)}px`;
        cursor.style.top = `${Math.max(0, y - normal / 2)}px`;
        };

        window.addEventListener('mousemove', (ev) => {
        if (!cfg.cursorEnabled) return;
        setCursor(ev.clientX, ev.clientY);
        emitTrail(ev.clientX, ev.clientY);
        }, true);

        window.__bridgeMoveCursor = (x, y) => {
        if (!cfg.cursorEnabled) return;
        setCursor(x, y);
        emitTrail(x, y);
        };

        window.__bridgeShowClick = (x, y, label) => {
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
            if (!bridgeControl && shouldCapture('click', bridgeControl)) {
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
) -> tuple[float, float] | None:
    last_exc: Exception | None = None
    for _ in range(4):
        try:
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
                page.evaluate(
                    "([x, y, label]) => window.__bridgeShowClick?.(x, y, label)",
                    [x, y, label],
                )
                if click_pulse_enabled:
                    page.evaluate("([x, y]) => window.__bridgePulseAt?.(x, y)", [x, y])
                page.wait_for_timeout(120)
                return (x, y)

            # Likely occluded by fixed UI (e.g., dock). Scroll up a bit and retry.
            try:
                page.evaluate("() => window.scrollBy(0, -220)")
            except Exception:
                pass
            try:
                page.wait_for_timeout(80)
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
    # More visible mouse path in visual mode: 30-60 steps depending on speed factor.
    steps = int(max(30, min(60, round(40 / max(0.3, speed)))))
    page.mouse.move(x, y, steps=steps)
    try:
        page.evaluate("([x, y]) => window.__bridgeMoveCursor?.(x, y)", [x, y])
    except Exception:
        pass


def _human_mouse_click(page: Any, x: float, y: float, *, speed: float, hold_ms: int) -> None:
    _human_mouse_move(page, x, y, speed=speed)
    try:
        page.evaluate("([x, y]) => window.__bridgePulseAt?.(x, y)", [x, y])
    except Exception:
        pass
    page.mouse.down()
    if hold_ms > 0:
        page.wait_for_timeout(hold_ms)
    page.mouse.up()


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
