"""Bootstrap helpers for web-run executor setup."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from bridge.web_watchdog import WebWatchdogConfig


@dataclass
class BrowserPageSetup:
    browser: Any
    context: Any
    page: Any
    attached: bool


@dataclass
class RunTimingConfig:
    step_hard_timeout_seconds: float
    run_hard_timeout_seconds: float
    run_deadline_ts: float
    wait_timeout_ms: int
    interactive_timeout_ms: int
    learning_window_seconds: int
    post_action_pause_ms: int
    watchdog_cfg: WebWatchdogConfig


def load_run_timing_config() -> RunTimingConfig:
    step_hard_timeout_seconds = max(
        0.1, float(os.getenv("BRIDGE_WEB_STEP_HARD_TIMEOUT_SECONDS", "20") or "20")
    )
    run_hard_timeout_seconds = max(
        0.1, float(os.getenv("BRIDGE_WEB_RUN_HARD_TIMEOUT_SECONDS", "120") or "120")
    )
    run_started_at = time.monotonic()
    run_deadline_ts = run_started_at + run_hard_timeout_seconds

    wait_timeout_ms = int(float(os.getenv("BRIDGE_WEB_WAIT_TIMEOUT_SECONDS", "12")) * 1000)
    wait_timeout_ms = max(1000, min(60000, wait_timeout_ms))
    interactive_timeout_ms = int(float(os.getenv("BRIDGE_WEB_INTERACTIVE_TIMEOUT_SECONDS", "8")) * 1000)
    interactive_timeout_ms = max(1000, min(60000, interactive_timeout_ms))
    learning_window_seconds = int(float(os.getenv("BRIDGE_LEARNING_WINDOW_SECONDS", "25")))
    post_action_pause_ms = int(float(os.getenv("BRIDGE_WEB_POST_ACTION_PAUSE_MS", "250")))
    post_action_pause_ms = max(0, min(2000, post_action_pause_ms))
    watchdog_cfg = WebWatchdogConfig(
        stuck_interactive_seconds=float(os.getenv("BRIDGE_WEB_STUCK_INTERACTIVE_SECONDS", "12")),
        stuck_step_seconds=float(os.getenv("BRIDGE_WEB_STUCK_STEP_SECONDS", "20")),
        stuck_iframe_seconds=float(os.getenv("BRIDGE_WEB_STUCK_IFRAME_SECONDS", "8")),
    )
    return RunTimingConfig(
        step_hard_timeout_seconds=step_hard_timeout_seconds,
        run_hard_timeout_seconds=run_hard_timeout_seconds,
        run_deadline_ts=run_deadline_ts,
        wait_timeout_ms=wait_timeout_ms,
        interactive_timeout_ms=interactive_timeout_ms,
        learning_window_seconds=learning_window_seconds,
        post_action_pause_ms=post_action_pause_ms,
        watchdog_cfg=watchdog_cfg,
    )


def setup_browser_page(
    *,
    playwright_obj: Any,
    session: Any,
    url: str,
    visual: bool,
    visual_mouse_speed: float,
    timeout_seconds: int,
    launch_browser: Callable[..., Any],
    mark_controlled: Callable[..., Any],
    safe_page_title: Callable[[Any], str],
) -> BrowserPageSetup:
    browser = None
    page = None
    context = None
    attached = session is not None
    if attached:
        browser = playwright_obj.chromium.connect_over_cdp(f"http://127.0.0.1:{session.port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        mark_controlled(session, True, url=page.url, title=safe_page_title(page))
    else:
        browser = launch_browser(
            playwright_obj,
            visual=visual,
            visual_mouse_speed=visual_mouse_speed,
        )
        page = browser.new_page()
    page.set_default_timeout(min(timeout_seconds * 1000, 120000))
    return BrowserPageSetup(browser=browser, context=context, page=page, attached=attached)


def install_visual_overlay_initial(
    *,
    page: Any,
    visual: bool,
    attached: bool,
    visual_cursor: bool,
    visual_click_pulse: bool,
    visual_scale: float,
    visual_color: str,
    session: Any,
    ui_findings: list[str],
    overlay_debug_path: Path,
    install_visual_overlay: Callable[..., None],
    session_state_payload: Callable[[Any], dict[str, Any]],
    ensure_visual_overlay_ready_best_effort: Callable[..., bool],
) -> None:
    if not visual:
        return
    try:
        install_visual_overlay(
            page,
            cursor_enabled=visual_cursor,
            click_pulse_enabled=visual_click_pulse,
            scale=visual_scale,
            color=visual_color,
            trace_enabled=False,
            session_state=session_state_payload(session),
        )
        page.bring_to_front()
    except Exception as exc:
        ui_findings.append(f"visual overlay install failed; degraded mode: {exc}")

    if attached:
        ensure_visual_overlay_ready_best_effort(
            page,
            ui_findings,
            cursor_expected=visual_cursor,
            retries=3,
            delay_ms=140,
            debug_screenshot_path=overlay_debug_path,
            force_reinit=True,
        )


def attach_page_observers(
    *,
    page: Any,
    console_errors: list[str],
    network_findings: list[str],
) -> None:
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


def apply_runtime_page_timeout(
    *,
    page: Any,
    timeout_seconds: int,
    run_hard_timeout_seconds: float,
) -> None:
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
