"""Deterministic web interaction backend using Playwright."""

from __future__ import annotations

import importlib.util
import json
import re
import socket
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from bridge.models import OIReport
from bridge.web_session import WebSession, mark_controlled


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

    return _execute_playwright(
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
    )


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
        try:
            initial_url = page.url
            initial_title = _safe_page_title(page)
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
                    )

            if visual:
                _ensure_visual_overlay_ready_best_effort(
                    page,
                    ui_findings,
                    cursor_expected=visual_cursor,
                    retries=3,
                    delay_ms=140,
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
                observations.append("Login state detected: Entrar demo present and enabled")
                # Insert a native optional step at the front (keeps evidence before/after machinery).
                steps = [WebStep("maybe_click_text", "Entrar demo")] + steps
            else:
                observations.append("demo not present; already authed")
                ui_findings.append("demo not present; already authed")

            interactive_step = 0
            total = len(steps)
            for idx, step in enumerate(steps, start=1):
                if progress_cb:
                    progress_cb(idx, total, f"web step {idx}/{total}: {step.kind}")

                if step.kind in (
                    "click_selector",
                    "click_text",
                    "maybe_click_text",
                    "select_label",
                    "select_value",
                ):
                    interactive_step += 1
                    before = evidence_dir / f"step_{interactive_step}_before.png"
                    after = evidence_dir / f"step_{interactive_step}_after.png"
                    page.screenshot(path=str(before), full_page=True)
                    evidence_paths.append(_to_repo_rel(before))
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
                    )
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
                        )
                    continue

                try:
                    _apply_wait_step(
                        page,
                        step,
                        idx,
                        actions,
                        observations,
                        ui_findings,
                        timeout_ms=wait_timeout_ms,
                    )
                except Exception as exc:
                    if _is_timeout_error(exc):
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
                    )
        finally:
            if visual and control_enabled:
                _set_assistant_control_overlay(page, False)
                if session is not None:
                    _update_top_bar_state(
                        page,
                        _session_state_payload(session, override_controlled=False),
                    )
                ui_findings.append("control released")
            if attached and session is not None:
                mark_controlled(session, False, url=page.url, title=_safe_page_title(page))
            if not attached and not keep_open:
                browser.close()

    result = locals().get("result", "success")
    if result != "failed" and (console_errors or network_findings):
        result = "partial"
    if verified and steps and not ui_findings:
        raise SystemExit("Verified web mode requires post-step visible verification findings.")

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
) -> None:
    if step.kind == "click_selector":
        actions.append(f"cmd: playwright click selector:{step.target}")
        locator = page.locator(step.target).first
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
                locator.click()
        else:
            locator.click()
        observations.append(f"Clicked selector in step {step_num}: {step.target}")
        ui_findings.append(f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}")
        return

    if step.kind == "click_text":
        actions.append(f"cmd: playwright click text:{step.target}")
        locator = page.get_by_text(step.target, exact=False).first
        try:
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
                    locator.click()
            else:
                locator.click()
            observations.append(f"Clicked text in step {step_num}: {step.target}")
            ui_findings.append(
                f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}"
            )
            return
        except Exception:
            if str(step.target).strip().lower() == "reproducir":
                fallback = page.locator('.track-card:has-text("Stan") button:has-text("Reproducir")').first
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
                        fallback.click()
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
        actions.append(f"cmd: playwright maybe click text:{step.target}")
        locator = page.get_by_text(step.target, exact=False).first
        try:
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
                locator.click()
            observations.append(f"Maybe clicked text in step {step_num}: {step.target}")
            ui_findings.append(f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}")
            return
        except Exception:
            observations.append(f"Step {step_num}: maybe click not present: {step.target}")
            ui_findings.append(f"step {step_num} verify optional click skipped: {step.target}")
            return

    if step.kind == "select_label":
        actions.append(f"cmd: playwright select selector:{step.target} label:{step.value}")
        locator = page.locator(step.target).first
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
        observations.append(
            f"Selected option by label in step {step_num}: selector={step.target}, label={step.value}"
        )
        ui_findings.append(f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}")
        return

    if step.kind == "select_value":
        actions.append(f"cmd: playwright select selector:{step.target} value:{step.value}")
        locator = page.locator(step.target).first
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
        observations.append(
            f"Selected option by value in step {step_num}: selector={step.target}, value={step.value}"
        )
        ui_findings.append(f"step {step_num} verify visible result: url={page.url}, title={_safe_page_title(page)}")
        return

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
    if step.kind == "wait_selector":
        actions.append(f"cmd: playwright wait selector:{step.target}")
        page.wait_for_selector(step.target, timeout=timeout_ms)
        observations.append(f"Wait selector step {step_num}: {step.target}")
        ui_findings.append(f"step {step_num} verify selector visible: {step.target}")
        return
    if step.kind == "wait_text":
        actions.append(f"cmd: playwright wait text:{step.target}")
        page.get_by_text(step.target, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
        observations.append(f"Wait text step {step_num}: {step.target}")
        ui_findings.append(f"step {step_num} verify text visible: {step.target}")
        return
    raise RuntimeError(f"Unsupported wait step kind: {step.kind}")


def _is_login_target(text: str) -> bool:
    low = text.lower().strip()
    return low in ("entrar demo", "entrar", "login", "sign in", "iniciar sesión")


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
        if (!root) {
          if (!window.__bridgeOverlayRetryAttached) {
            window.__bridgeOverlayRetryAttached = true;
            document.addEventListener('DOMContentLoaded', () => {
              installOverlay();
            }, { once: true });
          }
          return false;
        }
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
        root.appendChild(cursor);
        const trailLayer = document.createElement('div');
        trailLayer.id = '__bridge_trail_layer';
        trailLayer.style.position = 'fixed';
        trailLayer.style.inset = '0';
        trailLayer.style.pointerEvents = 'none';
        trailLayer.style.zIndex = '2147483646';
        root.appendChild(trailLayer);

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
        root.appendChild(stateBorder);

        window.__bridgeSetStateBorder = (state) => {
          const s = state || {};
          const controlled = !!s.controlled;
          const open = String(s.state || 'open') === 'open';
          const incidentOpen = !!s.incident_open;
          const controlUrl = window.__bridgeResolveControlUrl ? window.__bridgeResolveControlUrl(s) : null;
          const agentOnline = !!controlUrl && s.agent_online !== false;
          const readyManual = open && !controlled && agentOnline && !incidentOpen;

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
          document.addEventListener('click', (ev) => {
            const el = ev.target;
            let target = '';
            if (el && typeof el.closest === 'function') {
              const btn = el.closest('button,[role="button"],a,input,select,textarea');
              if (btn) target = (btn.textContent || btn.id || btn.className || '').trim();
            }
            window.__bridgeSendSessionEvent({
              type: 'click',
              target,
              message: `click ${target}`,
              x: Number(ev.clientX || 0),
              y: Number(ev.clientY || 0),
            });
          }, true);
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
            root.appendChild(hot);
            root.appendChild(toggle);
            root.appendChild(bar);
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
          const readyManual = open && !controlled && agentOnline && !incidentOpen;
          const incidentText = String(s.last_error || '').slice(0, 96);
          bar.style.background = controlled
            ? 'rgba(59,167,255,0.22)'
            : (
              incidentOpen
                ? 'rgba(255,82,82,0.26)'
                : (
                  readyManual
                    ? 'rgba(22,163,74,0.22)'
                    : (open ? 'rgba(80,80,80,0.28)' : 'rgba(20,20,20,0.7)')
                )
            );
          bar.style.borderBottom = readyManual
            ? '2px solid rgba(34,197,94,0.95)'
            : '1px solid rgba(255,255,255,0.18)';
          bar.dataset.state = JSON.stringify(s);
          window.__bridgeSetIncidentOverlay(incidentOpen && !controlled, incidentText || 'INCIDENT DETECTED');
          window.__bridgeSetStateBorder?.(s);
          window.__bridgeEnsureSessionObserver();
          window.__bridgeStartTopBarPolling(s);
          const ctrl = controlled ? 'assistant' : 'user';
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
    try:
        ok = bool(
            page.evaluate(
                """
                () => {
                  const el = document.getElementById('__bridge_cursor_overlay');
                  if (!el) return false;
                  const style = window.getComputedStyle(el);
                  return style.display !== 'none' && style.visibility !== 'hidden';
                }
                """
            )
        )
    except Exception:
        ok = False
    if not ok:
        raise RuntimeError(
            "Visual overlay not visible: missing #__bridge_cursor_overlay or display is none."
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
) -> bool:
    # Force re-injection / re-enable in attach flows and after navigations.
    last_error: BaseException | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            _ensure_visual_overlay_installed(page)
            if cursor_expected:
                try:
                    _verify_visual_overlay_visible(page)
                    return True
                except BaseException as exc:
                    last_error = exc
            else:
                return True
        except BaseException as exc:
            last_error = exc
        try:
            page.wait_for_timeout(delay_ms)
        except Exception:
            pass
        ui_findings.append(f"visual overlay retry {attempt}/{retries}")

    ui_findings.append(
        "visual overlay degraded: cursor overlay not visible; continuing without cursor"
    )
    if last_error is not None:
        ui_findings.append(f"visual overlay error: {last_error}")
    return False


def _set_assistant_control_overlay(page: Any, enabled: bool) -> None:
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


def _session_state_payload(
    session: WebSession | None,
    *,
    override_controlled: bool | None = None,
    override_state: str | None = None,
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
        "last_seen_at": session.last_seen_at,
        "control_port": control_port,
        "control_url": f"http://127.0.0.1:{control_port}" if control_port > 0 else "",
        "agent_online": control_port > 0,
    }


def _update_top_bar_state(page: Any, payload: dict[str, Any]) -> None:
    page.evaluate("([payload]) => window.__bridgeUpdateTopBarState?.(payload)", [payload])


def _destroy_top_bar(page: Any) -> None:
    page.evaluate("() => window.__bridgeDestroyTopBar?.()")


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
