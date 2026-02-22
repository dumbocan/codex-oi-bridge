"""Step-runner helpers for web-run loop bookkeeping."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from bridge.web_steps import WebStep


def record_step_outcome(
    *,
    step_outcomes: list[dict[str, Any]],
    ui_findings: list[str],
    idx: int,
    step: WebStep,
    status: str,
    reason: str = "",
) -> None:
    payload: dict[str, Any] = {
        "index": int(idx),
        "kind": str(step.kind),
        "target": str(step.target),
        "status": str(status),
    }
    if reason:
        payload["reason"] = str(reason)
    step_outcomes.append(payload)
    ui_findings.append(f"step_status={json.dumps(payload, ensure_ascii=False)}")


def append_skipped_not_applicable(
    *,
    observations: list[str],
    ui_findings: list[str],
    idx: int,
    step: WebStep,
    skip_reason: str,
) -> None:
    observations.append(
        f"Step {idx}: skipped_not_applicable {step.kind}:{step.target} ({skip_reason})"
    )
    ui_findings.append(
        f"step {idx} skipped_not_applicable: {step.kind}:{step.target} reason={skip_reason}"
    )


def apply_step_common_prechecks(
    *,
    page: Any,
    session: Any | None,
    step: WebStep,
    idx: int,
    total: int,
    run_deadline_ts: float,
    step_hard_timeout_seconds: float,
    watchdog_step_signature: str,
    teaching_mode: bool,
    visual: bool,
    visual_cursor: bool,
    ui_findings: list[str],
    overlay_debug_path: str,
    remaining_ms: Any,
    runtime_closed: Any,
    append_run_crash_findings: Any,
    trigger_timeout_handoff: Any,
    watchdog_stuck_attempt: Any,
    progress_cb: Any,
    ensure_visual_overlay_ready: Any,
) -> tuple[bool, bool]:
    if runtime_closed(page, session):
        append_run_crash_findings(ui_findings)
        return True, True
    if time.monotonic() > run_deadline_ts:
        if trigger_timeout_handoff(
            what_failed="run_timeout",
            where=watchdog_step_signature or "web-run",
            learning_target="",
            attempted="run hard timeout exceeded",
            why_likely=(
                "run exceeded BRIDGE_WEB_RUN_HARD_TIMEOUT_SECONDS without completing all steps"
            ),
            notice_message="He excedido el tiempo máximo del run. Te cedo el control.",
        ):
            return True, False
    try:
        step_budget_ms = max(
            800,
            min(
                int(step_hard_timeout_seconds * 1000),
                remaining_ms(run_deadline_ts),
            ),
        )
        page.set_default_timeout(step_budget_ms)
    except Exception:
        pass
    if teaching_mode and watchdog_stuck_attempt("watchdog:loop"):
        return True, False
    if progress_cb:
        progress_cb(idx, total, f"web step {idx}/{total}: {step.kind}")
    if visual:
        ensure_visual_overlay_ready(
            page,
            ui_findings,
            cursor_expected=visual_cursor,
            retries=3,
            delay_ms=120,
            debug_screenshot_path=overlay_debug_path,
            force_reinit=True,
        )
    return False, False


@dataclass(frozen=True)
class WaitStepResult:
    should_break: bool = False
    result: str = ""
    crashed: bool = False
    recorded_status: str = ""
    recorded_reason: str = ""


@dataclass(frozen=True)
class InteractiveStepResult:
    should_break: bool = False
    result: str = ""
    crashed: bool = False
    next_interactive_step: int = 0
    attempted_hint: str = ""
    recorded_status: str = ""
    action_progressed: bool = False
    learning_selector_used: str = ""


def execute_interactive_step(
    *,
    page: Any,
    step: WebStep,
    idx: int,
    total: int,
    current_interactive_step: int,
    teaching_mode: bool,
    interactive_timeout_ms: int,
    step_hard_timeout_seconds: float,
    run_deadline_ts: float,
    watchdog_step_signature: str,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    console_errors: list[str],
    remaining_ms: Any,
    trigger_timeout_handoff: Any,
    force_main_frame_context: Any,
    apply_iframe_precheck_handoff: Any,
    capture_evidence: Any,
    apply_interactive_step_with_retries: Any,
    apply_interactive_step: Any,
    on_retry_stuck_handoff: Any,
    on_target_not_found_handoff: Any,
    runtime_closed: Any,
    session: Any | None,
    is_page_closed_error: Any,
    is_timeout_error: Any,
    append_interactive_timeout_findings: Any,
) -> InteractiveStepResult:
    step_started_at = time.monotonic()
    step_deadline_ts = step_started_at + step_hard_timeout_seconds
    if min(remaining_ms(step_deadline_ts), remaining_ms(run_deadline_ts)) <= 0:
        if trigger_timeout_handoff(
            what_failed="interactive_timeout",
            where=watchdog_step_signature or f"step {idx}/{total}",
            learning_target=step.target,
            attempted="step hard timeout precheck",
            why_likely=(
                "interactive step exceeded BRIDGE_WEB_STEP_HARD_TIMEOUT_SECONDS before execution"
            ),
            notice_message="El paso interactivo superó el tiempo límite. Te cedo el control.",
        ):
            return InteractiveStepResult(should_break=True)
    if not force_main_frame_context(page):
        if apply_iframe_precheck_handoff(
            where=watchdog_step_signature,
            learning_target=step.target,
            why_likely="unable to return focus/context to main frame before interactive action",
        ):
            return InteractiveStepResult(should_break=True)
        raise RuntimeError("Unable to return to main frame context before interactive step")

    interactive_step = int(current_interactive_step) + 1
    capture_evidence(f"step_{interactive_step}_before.png")
    prev_action_len = len(actions)
    attempted_hint = ""
    learning_selector_used = ""
    try:
        effective_timeout_ms = min(
            interactive_timeout_ms,
            max(250, remaining_ms(step_deadline_ts)),
            max(250, remaining_ms(run_deadline_ts)),
        )
        if effective_timeout_ms <= 250 and (
            remaining_ms(step_deadline_ts) <= 0 or remaining_ms(run_deadline_ts) <= 0
        ):
            if trigger_timeout_handoff(
                what_failed="interactive_timeout",
                where=watchdog_step_signature or f"step {idx}/{total}",
                learning_target=step.target,
                attempted="step hard timeout in interactive execution",
                why_likely="interactive action budget exhausted before action started",
                notice_message="El paso interactivo superó el tiempo límite. Te cedo el control.",
            ):
                return InteractiveStepResult(should_break=True, next_interactive_step=interactive_step)
        if teaching_mode:
            retry_result = apply_interactive_step_with_retries(
                step=step,
                step_num=interactive_step,
                timeout_ms=effective_timeout_ms,
                step_label=f"web step {idx}/{total}: {step.kind}:{step.target}",
                step_deadline_ts=step_deadline_ts,
                run_deadline_ts=run_deadline_ts,
            )
            if bool(getattr(retry_result, "deadline_hit", False)):
                if trigger_timeout_handoff(
                    what_failed="interactive_timeout",
                    where=watchdog_step_signature or f"step {idx}/{total}",
                    learning_target=step.target,
                    attempted=retry_result.attempted or "step hard timeout",
                    why_likely=(
                        "interactive retries exceeded hard timeout without completing action"
                    ),
                    notice_message="El paso interactivo superó el tiempo límite. Te cedo el control.",
                ):
                    return InteractiveStepResult(should_break=True, next_interactive_step=interactive_step)
            if retry_result.stuck:
                if on_retry_stuck_handoff(retry_result.attempted, step.target):
                    return InteractiveStepResult(should_break=True, next_interactive_step=interactive_step)
            attempted_hint = retry_result.attempted
            learning_selector_used = str(getattr(retry_result, "selector_used", "")).strip()
        else:
            apply_interactive_step(
                step=step,
                step_num=interactive_step,
                timeout_ms=effective_timeout_ms,
            )
    except Exception as exc:
        if is_page_closed_error(exc) or runtime_closed(page, session):
            return InteractiveStepResult(
                should_break=True,
                result="failed",
                crashed=True,
                next_interactive_step=interactive_step,
            )
        if teaching_mode and on_target_not_found_handoff(
            step_kind=step.kind,
            step_target=step.target,
            interactive_step=interactive_step,
        ):
            return InteractiveStepResult(
                should_break=True,
                result="partial",
                next_interactive_step=interactive_step,
            )
        if is_timeout_error(exc):
            capture_evidence(f"step_{interactive_step}_timeout.png")
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
            return InteractiveStepResult(
                should_break=True,
                result="failed",
                next_interactive_step=interactive_step,
            )
        raise

    capture_evidence(f"step_{interactive_step}_after.png")
    return InteractiveStepResult(
        next_interactive_step=interactive_step,
        attempted_hint=attempted_hint,
        recorded_status="executed",
        action_progressed=len(actions) > prev_action_len,
        learning_selector_used=learning_selector_used,
    )


def execute_wait_step(
    *,
    page: Any,
    step: WebStep,
    idx: int,
    total: int,
    teaching_mode: bool,
    step_hard_timeout_seconds: float,
    run_deadline_ts: float,
    wait_timeout_ms: int,
    watchdog_step_signature: str,
    observations: list[str],
    ui_findings: list[str],
    console_errors: list[str],
    remaining_ms: Any,
    trigger_timeout_handoff: Any,
    force_main_frame_context: Any,
    apply_iframe_precheck_handoff: Any,
    apply_wait_step: Any,
    add_timeout_evidence: Any,
    runtime_closed: Any,
    session: Any | None,
    is_page_closed_error: Any,
    is_timeout_error: Any,
    should_soft_skip_wait_timeout: Any,
    append_wait_timeout_findings: Any,
) -> WaitStepResult:
    try:
        step_started_at = time.monotonic()
        step_deadline_ts = step_started_at + step_hard_timeout_seconds
        effective_wait_timeout_ms = min(
            wait_timeout_ms,
            max(250, remaining_ms(step_deadline_ts)),
            max(250, remaining_ms(run_deadline_ts)),
        )
        if effective_wait_timeout_ms <= 250 and (
            remaining_ms(step_deadline_ts) <= 0 or remaining_ms(run_deadline_ts) <= 0
        ):
            if trigger_timeout_handoff(
                what_failed="interactive_timeout",
                where=watchdog_step_signature or f"step {idx}/{total}",
                learning_target=step.target,
                attempted="step hard timeout before wait",
                why_likely="wait step deadline exceeded before operation",
                notice_message="El paso superó el tiempo límite. Te cedo el control.",
            ):
                return WaitStepResult(should_break=True)
        if not force_main_frame_context(page):
            if apply_iframe_precheck_handoff(
                where=watchdog_step_signature,
                learning_target=step.target,
                why_likely="unable to return focus/context to main frame before wait step",
            ):
                return WaitStepResult(should_break=True)
            raise RuntimeError("Unable to return to main frame context before wait step")
        apply_wait_step(
            page,
            step,
            idx,
            timeout_ms=effective_wait_timeout_ms,
        )
        return WaitStepResult(recorded_status="executed")
    except Exception as exc:
        if is_page_closed_error(exc) or runtime_closed(page, session):
            return WaitStepResult(should_break=True, result="failed", crashed=True)
        if is_timeout_error(exc):
            if should_soft_skip_wait_timeout(step=step, idx=idx, teaching_mode=teaching_mode):
                observations.append(
                    "teaching wait soft-skip: timed out on wait_text but next step is Stop"
                )
                ui_findings.append(
                    f"step {idx} soft-skip wait timeout on {step.kind}:{step.target} (teaching)"
                )
                return WaitStepResult(
                    recorded_status="skipped_not_applicable",
                    recorded_reason="teaching soft-skip wait timeout",
                )
            add_timeout_evidence(idx)
            console_errors.append(f"Timeout on step {idx}: {step.kind} {step.target}")
            append_wait_timeout_findings(
                ui_findings,
                step_num=idx,
                step_kind=step.kind,
                step_target=step.target,
                timeout_ms=wait_timeout_ms,
            )
            return WaitStepResult(should_break=True, result="failed")
        raise
