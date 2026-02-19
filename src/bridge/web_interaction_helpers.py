"""Shared helpers for interactive web steps."""

from __future__ import annotations

from typing import Any, Callable

from bridge.web_steps import WebStep


def retry_scroll(page: Any, *, amount: int = 180, pause_ms: int = 140) -> None:
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


def stable_selectors_for_target(target: str) -> list[str]:
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


def semantic_hints_for_selector(selector: str) -> list[str]:
    low = str(selector or "").strip().lower()
    if not low:
        return []
    hints: list[str] = []
    if "stop" in low:
        hints.append("Stop")
    if "play" in low or "reproducir" in low:
        hints.append("Reproducir")
    return hints


def apply_wait_step(
    page: Any,
    step: WebStep,
    step_num: int,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    *,
    timeout_ms: int,
    disable_active_youtube_iframe_pointer_events: Callable[[Any], dict[str, Any] | None],
    force_main_frame_context: Callable[[Any], bool],
    restore_iframe_pointer_events: Callable[[Any, dict[str, Any] | None], None],
) -> None:
    iframe_guard = disable_active_youtube_iframe_pointer_events(page)
    if not force_main_frame_context(page):
        restore_iframe_pointer_events(page, iframe_guard)
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
        restore_iframe_pointer_events(page, iframe_guard)
    raise RuntimeError(f"Unsupported wait step kind: {step.kind}")
