"""Deterministic web interaction backend using Playwright."""

from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from bridge.models import OIReport


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
) -> OIReport:
    url_match = _URL_RE.search(task)
    if not url_match:
        raise SystemExit("Web mode requires an explicit URL in task.")
    url = _normalize_url(url_match.group(0))
    if not _is_valid_url(url):
        raise SystemExit(f"Web mode received invalid URL token: {url_match.group(0)}")
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
    )


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
) -> OIReport:
    from playwright.sync_api import sync_playwright

    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    actions: list[str] = [f"cmd: playwright goto {url}"]
    if visual:
        actions.append("cmd: playwright visual on")
    observations: list[str] = []
    console_errors: list[str] = []
    network_findings: list[str] = []
    ui_findings: list[str] = []
    evidence_paths: list[str] = []

    with sync_playwright() as p:
        browser = _launch_browser(p, visual=visual)
        page = browser.new_page()
        page.set_default_timeout(min(timeout_seconds * 1000, 120000))
        if visual:
            _install_visual_overlay(
                page,
                cursor_enabled=visual_cursor,
                click_pulse_enabled=visual_click_pulse,
                scale=visual_scale,
                color=visual_color,
                trace_enabled=True,
            )
            page.bring_to_front()

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

        page.goto(url, wait_until="domcontentloaded")
        observations.append(f"Opened URL: {url}")
        observations.append(f"Page title: {page.title()}")

        interactive_step = 0
        total = len(steps)
        for idx, step in enumerate(steps, start=1):
            if progress_cb:
                progress_cb(idx, total, f"web step {idx}/{total}: {step.kind}")

            if step.kind in ("click_selector", "click_text", "select_label", "select_value"):
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
                )
                page.wait_for_timeout(1000)
                page.screenshot(path=str(after), full_page=True)
                evidence_paths.append(_to_repo_rel(after))
                continue

            _apply_wait_step(page, step, idx, actions, observations, ui_findings)

        browser.close()

    result = "success"
    if console_errors or network_findings:
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
) -> None:
    if step.kind == "click_selector":
        actions.append(f"cmd: playwright click selector:{step.target}")
        if visual:
            _highlight_target(
                page,
                page.locator(step.target).first,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled,
            )
        page.locator(step.target).first.click()
        observations.append(f"Clicked selector in step {step_num}: {step.target}")
        ui_findings.append(f"step {step_num} verify visible result: url={page.url}, title={page.title()}")
        return

    if step.kind == "click_text":
        actions.append(f"cmd: playwright click text:{step.target}")
        try:
            if visual:
                _highlight_target(
                    page,
                    page.get_by_text(step.target, exact=False).first,
                    f"step {step_num}",
                    click_pulse_enabled=click_pulse_enabled,
                )
            page.get_by_text(step.target, exact=False).first.click()
            observations.append(f"Clicked text in step {step_num}: {step.target}")
            ui_findings.append(
                f"step {step_num} verify visible result: url={page.url}, title={page.title()}"
            )
            return
        except Exception:
            if _is_login_target(step.target) and _looks_authenticated(page):
                observations.append(
                    f"Step {step_num}: target '{step.target}' not found; authenticated state detected."
                )
                ui_findings.append(
                    f"step {step_num} verify authenticated session already active"
                )
                return
            raise

    if step.kind == "select_label":
        actions.append(f"cmd: playwright select selector:{step.target} label:{step.value}")
        if visual:
            _highlight_target(
                page,
                page.locator(step.target).first,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled,
            )
        page.locator(step.target).first.select_option(label=step.value)
        observations.append(
            f"Selected option by label in step {step_num}: selector={step.target}, label={step.value}"
        )
        ui_findings.append(f"step {step_num} verify visible result: url={page.url}, title={page.title()}")
        return

    if step.kind == "select_value":
        actions.append(f"cmd: playwright select selector:{step.target} value:{step.value}")
        if visual:
            _highlight_target(
                page,
                page.locator(step.target).first,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled,
            )
        page.locator(step.target).first.select_option(value=step.value)
        observations.append(
            f"Selected option by value in step {step_num}: selector={step.target}, value={step.value}"
        )
        ui_findings.append(f"step {step_num} verify visible result: url={page.url}, title={page.title()}")
        return

    raise RuntimeError(f"Unsupported interactive step kind: {step.kind}")


def _apply_wait_step(
    page: Any,
    step: WebStep,
    step_num: int,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
) -> None:
    if step.kind == "wait_selector":
        actions.append(f"cmd: playwright wait selector:{step.target}")
        page.wait_for_selector(step.target)
        observations.append(f"Wait selector step {step_num}: {step.target}")
        ui_findings.append(f"step {step_num} verify selector visible: {step.target}")
        return
    if step.kind == "wait_text":
        actions.append(f"cmd: playwright wait text:{step.target}")
        page.get_by_text(step.target, exact=False).first.wait_for(state="visible")
        observations.append(f"Wait text step {step_num}: {step.target}")
        ui_findings.append(f"step {step_num} verify text visible: {step.target}")
        return
    raise RuntimeError(f"Unsupported wait step kind: {step.kind}")


def _is_login_target(text: str) -> bool:
    low = text.lower().strip()
    return low in ("entrar demo", "entrar", "login", "sign in", "iniciar sesión")


def _looks_authenticated(page: Any) -> bool:
    for hint in _AUTH_HINTS:
        try:
            if page.get_by_text(hint, exact=False).count() > 0:
                return True
        except Exception:
            continue
    return False


def _launch_browser(playwright_obj: Any, *, visual: bool = False) -> Any:
    kwargs: dict[str, Any] = {"headless": not visual}
    if visual:
        kwargs["slow_mo"] = 120
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
) -> None:
    config = {
        "cursorEnabled": bool(cursor_enabled),
        "clickPulseEnabled": bool(click_pulse_enabled),
        "scale": float(scale),
        "color": str(color),
        "traceEnabled": bool(trace_enabled),
    }
    script_template = """
    (() => {
      if (window.__bridgeOverlayInstalled) return;
      const cfg = __CFG_JSON__;
      window.__bridgeOverlayInstalled = true;
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
      document.documentElement.appendChild(cursor);
      const trailLayer = document.createElement('div');
      trailLayer.id = '__bridge_trail_layer';
      trailLayer.style.position = 'fixed';
      trailLayer.style.inset = '0';
      trailLayer.style.pointerEvents = 'none';
      trailLayer.style.zIndex = '2147483646';
      document.documentElement.appendChild(trailLayer);

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

      window.__bridgeShowClick = (x, y, label) => {
        if (cfg.cursorEnabled) {
          setCursor(x, y);
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
        ring.style.transition = 'transform 300ms ease, opacity 300ms ease';
        document.documentElement.appendChild(ring);
        requestAnimationFrame(() => {
          ring.style.transform = 'scale(2.1)';
          ring.style.opacity = '0';
        });
        setTimeout(() => ring.remove(), 340);
      };
    })();
    """
    script = script_template.replace("__CFG_JSON__", json.dumps(config, ensure_ascii=False))
    page.add_init_script(script)


def _highlight_target(
    page: Any,
    locator: Any,
    label: str,
    *,
    click_pulse_enabled: bool,
) -> None:
    try:
        box = locator.bounding_box()
        if not box:
            return
        x = float(box.get("x", 0.0)) + float(box.get("width", 0.0)) / 2.0
        y = float(box.get("y", 0.0)) + float(box.get("height", 0.0)) / 2.0
        page.evaluate("([x, y, label]) => window.__bridgeShowClick?.(x, y, label)", [x, y, label])
        if click_pulse_enabled:
            page.evaluate("([x, y]) => window.__bridgePulseAt?.(x, y)", [x, y])
        page.wait_for_timeout(120)
    except Exception:
        return


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
