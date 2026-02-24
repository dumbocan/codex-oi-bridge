"""Retry orchestration for interactive web steps."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from bridge.web_steps import WebStep


@dataclass(frozen=True)
class RetryResult:
    selector_used: str = ""
    stuck: bool = False
    attempted: str = ""
    deadline_hit: bool = False


def apply_interactive_step_with_retries(
    *,
    page: Any,
    step: WebStep,
    step_num: int,
    evidence_dir: Path,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    evidence_paths: list[str],
    visual: bool,
    click_pulse_enabled: bool,
    visual_human_mouse: bool,
    visual_mouse_speed: float,
    visual_click_hold_ms: int,
    timeout_ms: int,
    max_retries: int,
    learning_selectors: list[str],
    session: Any | None,
    step_label: str,
    stuck_interactive_seconds: float,
    stuck_step_seconds: float,
    step_deadline_ts: float,
    run_deadline_ts: float,
    to_repo_rel: Callable[[Path], str],
    observer_useful_event_count: Callable[[Any | None], int],
    retry_scroll: Callable[[Any], None],
    apply_interactive_step: Callable[..., None],
    is_generic_play_label: Callable[[str], bool],
    stable_selectors_for_target: Callable[[str], list[str]],
    is_specific_selector: Callable[[str], bool],
    semantic_hints_for_selector: Callable[[str], list[str]],
) -> RetryResult:
    candidates: list[WebStep] = [step]
    if step.kind == "click_text":
        if not is_generic_play_label(step.target):
            for selector in stable_selectors_for_target(step.target):
                candidates.append(WebStep("click_selector", selector))
            for selector in learning_selectors:
                candidates.insert(1, WebStep("click_selector", selector))
    elif step.kind == "click_selector":
        for selector in learning_selectors:
            candidates.insert(0, WebStep("click_selector", selector))
        if not is_specific_selector(step.target):
            for hint in semantic_hints_for_selector(step.target):
                candidates.append(WebStep("click_text", hint))
                for selector in stable_selectors_for_target(hint):
                    candidates.append(WebStep("click_selector", selector))

    last_exc: BaseException | None = None
    total_attempts = max(1, int(max_retries) + 1)
    started_at = time.monotonic()
    baseline_events = observer_useful_event_count(session)
    attempted_parts: list[str] = []
    for attempt in range(1, total_attempts + 1):
        now = time.monotonic()
        if now > step_deadline_ts or now > run_deadline_ts:
            attempted = ", ".join((attempted_parts + ["deadline=step_or_run"])[:18])
            return RetryResult(selector_used="", stuck=False, attempted=attempted, deadline_hit=True)
        attempted_parts.append(f"retry={attempt - 1}")
        if attempt > 1:
            retry_scroll(page)
            attempted_parts.append("scroll=main+page")
            ui_findings.append(f"step {step_num} retry {attempt - 1}/{max_retries}: scrolled and re-attempting")
        before_retry = evidence_dir / f"step_{step_num}_retry_{attempt}_before.png"
        after_retry = evidence_dir / f"step_{step_num}_retry_{attempt}_after.png"
        try:
            page.screenshot(path=str(before_retry), full_page=False)
            evidence_paths.append(to_repo_rel(before_retry))
        except Exception:
            pass
        for candidate in candidates:
            now = time.monotonic()
            if now > step_deadline_ts or now > run_deadline_ts:
                attempted = ", ".join((attempted_parts + ["deadline=step_or_run"])[:18])
                return RetryResult(selector_used="", stuck=False, attempted=attempted, deadline_hit=True)
            try:
                if candidate.kind == "click_selector":
                    attempted_parts.append(f"selector={candidate.target}")
                apply_interactive_step(
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
                    evidence_paths.append(to_repo_rel(after_retry))
                except Exception:
                    pass
                if candidate.kind == "click_selector" and candidate.target != step.target:
                    observations.append(
                        f"step {step_num} used stable selector fallback: {candidate.target}"
                    )
                    return RetryResult(selector_used=candidate.target)
                return RetryResult(selector_used="")
            except (Exception, SystemExit) as exc:
                last_exc = exc
                if _should_mark_stuck(
                    started_at=started_at,
                    session=session,
                    baseline_useful_events=baseline_events,
                    stuck_interactive_seconds=stuck_interactive_seconds,
                    stuck_step_seconds=stuck_step_seconds,
                    observer_useful_event_count=observer_useful_event_count,
                ):
                    attempted = ", ".join(attempted_parts[-18:])
                    ui_findings.append(
                        f"stuck detected on {step_label}: elapsed>{stuck_interactive_seconds}s "
                        "and no useful observer events"
                    )
                    return RetryResult(selector_used="", stuck=True, attempted=attempted)
                continue
        try:
            page.screenshot(path=str(after_retry), full_page=False)
            evidence_paths.append(to_repo_rel(after_retry))
        except Exception:
            pass
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Failed interactive step after retries: {step.kind} {step.target}")


def _should_mark_stuck(
    *,
    started_at: float,
    session: Any | None,
    baseline_useful_events: int,
    stuck_interactive_seconds: float,
    stuck_step_seconds: float,
    observer_useful_event_count: Callable[[Any | None], int],
) -> bool:
    elapsed = max(0.0, time.monotonic() - started_at)
    no_useful_events = True
    current_useful = observer_useful_event_count(session)
    if current_useful > baseline_useful_events:
        no_useful_events = False

    if elapsed > max(0.1, float(stuck_step_seconds)):
        return True
    if elapsed > max(0.1, float(stuck_interactive_seconds)) and no_useful_events:
        return True
    return False
