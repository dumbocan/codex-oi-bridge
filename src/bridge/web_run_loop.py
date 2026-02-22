"""Step-loop orchestration extracted from the web backend executor."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class StepLoopResult:
    step_outcomes: list[dict[str, Any]]


def execute_steps_loop(
    *,
    page: Any,
    steps: list[Any],
    session: Any,
    run_state: Any,
    watchdog_state: Any,
    run_deadline_ts: float,
    step_hard_timeout_seconds: float,
    interactive_timeout_ms: int,
    wait_timeout_ms: int,
    learning_window_seconds: int,
    post_action_pause_ms: int,
    visual: bool,
    visual_cursor: bool,
    visual_click_pulse: bool,
    visual_human_mouse: bool,
    visual_mouse_speed: float,
    visual_click_hold_ms: int,
    teaching_mode: bool,
    progress_cb: Callable[..., Any] | None,
    overlay_debug_path: Path,
    evidence_dir: Path,
    learned_selector_map: dict[str, Any],
    learning_context: dict[str, str],
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    console_errors: list[str],
    evidence_paths: list[str],
    learning_notes: list[str],
    stuck_interactive_seconds: float,
    stuck_step_seconds: float,
    interactive_step_kinds: set[str],
    step_learning_target: Callable[[str, str], str],
    update_step_signature: Callable[..., None],
    apply_step_common_prechecks: Callable[..., tuple[bool, bool]],
    interactive_step_not_applicable_reason: Callable[..., str],
    append_skipped_not_applicable: Callable[..., None],
    record_step_outcome: Callable[..., None],
    execute_interactive_step: Callable[..., Any],
    execute_wait_step: Callable[..., Any],
    evaluate_iframe_precheck_handoff: Callable[..., Any],
    show_custom_handoff_notice: Callable[..., None],
    append_iframe_focus_findings: Callable[..., None],
    capture_timeout_evidence: Callable[..., None],
    apply_interactive_step_with_retries: Callable[..., Any],
    apply_interactive_step: Callable[..., None],
    learned_selectors_for_step: Callable[..., list[str]],
    retry_stuck_handoff: Callable[..., dict[str, Any]],
    target_not_found_handoff: Callable[..., dict[str, Any]],
    should_soft_skip_wait_timeout: Callable[..., bool],
    apply_wait_step: Callable[..., None],
    append_run_crash_findings: Callable[..., None],
    append_interactive_timeout_findings: Callable[..., None],
    append_wait_timeout_findings: Callable[..., None],
    ensure_visual_overlay_ready_best_effort: Callable[..., bool],
    remaining_ms: Callable[[float], int],
    trigger_timeout_handoff: Callable[..., bool],
    watchdog_stuck_attempt: Callable[[str], bool],
    apply_handoff_decision: Callable[[Any], bool],
    apply_handoff_updates: Callable[[dict[str, Any]], bool],
    force_main_frame_context: Callable[..., bool],
    runtime_closed: Callable[..., bool],
    is_page_closed_error: Callable[..., bool],
    is_timeout_error: Callable[..., bool],
    trigger_stuck_handoff: Callable[..., bool],
    show_teaching_notice: Callable[[Any, str], None],
    store_learned_selector: Callable[..., None],
) -> StepLoopResult:
    interactive_step = 0
    total = len(steps)
    step_outcomes: list[dict[str, Any]] = []

    for idx, step in enumerate(steps, start=1):
        attempted_hint = ""
        step_sig = f"step {idx}/{total} {step.kind}:{step.target}"
        step_learning = step_learning_target(step.kind, step.target)
        update_step_signature(
            watchdog_state,
            step_signature=step_sig,
            learning_target=step_learning,
            now_ts=time.monotonic(),
        )

        should_break, crashed = apply_step_common_prechecks(
            page=page,
            session=session,
            step=step,
            idx=idx,
            total=total,
            run_deadline_ts=run_deadline_ts,
            step_hard_timeout_seconds=step_hard_timeout_seconds,
            watchdog_step_signature=watchdog_state.current_step_signature,
            teaching_mode=teaching_mode,
            visual=visual,
            visual_cursor=visual_cursor,
            ui_findings=ui_findings,
            overlay_debug_path=overlay_debug_path,
            remaining_ms=remaining_ms,
            runtime_closed=runtime_closed,
            append_run_crash_findings=append_run_crash_findings,
            trigger_timeout_handoff=trigger_timeout_handoff,
            watchdog_stuck_attempt=watchdog_stuck_attempt,
            progress_cb=progress_cb,
            ensure_visual_overlay_ready=ensure_visual_overlay_ready_best_effort,
        )
        if should_break:
            if crashed:
                run_state.result = "failed"
            break

        if step.kind in interactive_step_kinds:
            skip_reason = interactive_step_not_applicable_reason(page, step)
            if skip_reason:
                append_skipped_not_applicable(
                    observations=observations,
                    ui_findings=ui_findings,
                    idx=idx,
                    step=step,
                    skip_reason=skip_reason,
                )
                record_step_outcome(
                    step_outcomes=step_outcomes,
                    ui_findings=ui_findings,
                    idx=idx,
                    step=step,
                    status="skipped_not_applicable",
                    reason=skip_reason,
                )
                watchdog_state.last_progress_event_ts = time.monotonic()
                continue

            interactive_result = execute_interactive_step(
                page=page,
                step=step,
                idx=idx,
                total=total,
                current_interactive_step=interactive_step,
                teaching_mode=teaching_mode,
                interactive_timeout_ms=interactive_timeout_ms,
                step_hard_timeout_seconds=step_hard_timeout_seconds,
                run_deadline_ts=run_deadline_ts,
                watchdog_step_signature=watchdog_state.current_step_signature,
                actions=actions,
                observations=observations,
                ui_findings=ui_findings,
                console_errors=console_errors,
                remaining_ms=remaining_ms,
                trigger_timeout_handoff=trigger_timeout_handoff,
                force_main_frame_context=force_main_frame_context,
                apply_iframe_precheck_handoff=lambda **kwargs: apply_handoff_decision(
                    evaluate_iframe_precheck_handoff(
                        page=page,
                        teaching_mode=teaching_mode,
                        attempted="main-frame-first precheck failed",
                        ui_findings=ui_findings,
                        show_custom_notice=show_custom_handoff_notice,
                        append_iframe_focus_findings=append_iframe_focus_findings,
                        control_enabled=run_state.control_enabled,
                        **kwargs,
                    )
                ),
                capture_evidence=lambda name: capture_timeout_evidence(
                    page=page,
                    evidence_dir=evidence_dir,
                    evidence_paths=evidence_paths,
                    name=name,
                ),
                apply_interactive_step_with_retries=lambda **kwargs: apply_interactive_step_with_retries(
                    page,
                    kwargs["step"],
                    kwargs["step_num"],
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
                    timeout_ms=kwargs["timeout_ms"],
                    max_retries=2,
                    learning_selectors=learned_selectors_for_step(
                        kwargs["step"], learned_selector_map, learning_context
                    ),
                    session=session,
                    step_label=kwargs["step_label"],
                    stuck_interactive_seconds=stuck_interactive_seconds,
                    stuck_step_seconds=stuck_step_seconds,
                    step_deadline_ts=kwargs["step_deadline_ts"],
                    run_deadline_ts=kwargs["run_deadline_ts"],
                ),
                apply_interactive_step=lambda **kwargs: apply_interactive_step(
                    page,
                    kwargs["step"],
                    kwargs["step_num"],
                    actions,
                    observations,
                    ui_findings,
                    visual=visual,
                    click_pulse_enabled=visual_click_pulse,
                    visual_human_mouse=visual_human_mouse,
                    visual_mouse_speed=visual_mouse_speed,
                    visual_click_hold_ms=visual_click_hold_ms,
                    timeout_ms=kwargs["timeout_ms"],
                    movement_capture_dir=evidence_dir,
                    evidence_paths=evidence_paths,
                ),
                on_retry_stuck_handoff=lambda attempted, step_target: apply_handoff_updates(
                    retry_stuck_handoff(
                        step_signature=watchdog_state.current_step_signature,
                        attempted=attempted,
                        target=step_target,
                        session=session,
                        page=page,
                        visual=visual,
                        learning_window_seconds=learning_window_seconds,
                        actions=actions,
                        ui_findings=ui_findings,
                        trigger_stuck_handoff=trigger_stuck_handoff,
                        control_enabled=run_state.control_enabled,
                    )
                ),
                on_target_not_found_handoff=lambda **kwargs: apply_handoff_updates(
                    target_not_found_handoff(
                        teaching_mode=teaching_mode,
                        step_kind=kwargs["step_kind"],
                        step_target=kwargs["step_target"],
                        interactive_step=kwargs["interactive_step"],
                        learning_notes=learning_notes,
                        ui_findings=ui_findings,
                        page=page,
                        show_teaching_notice=show_teaching_notice,
                    )
                ),
                runtime_closed=runtime_closed,
                session=session,
                is_page_closed_error=is_page_closed_error,
                is_timeout_error=is_timeout_error,
                append_interactive_timeout_findings=append_interactive_timeout_findings,
            )
            interactive_step = interactive_result.next_interactive_step
            if interactive_result.should_break:
                if interactive_result.result:
                    run_state.result = interactive_result.result
                if interactive_result.crashed:
                    append_run_crash_findings(ui_findings)
                break
            attempted_hint = interactive_result.attempted_hint
            if interactive_result.learning_selector_used:
                learning_notes.append(
                    f"selector used for target '{step.target}': "
                    f"{interactive_result.learning_selector_used}"
                )
                store_learned_selector(
                    target=step.target,
                    selector=interactive_result.learning_selector_used,
                    context=learning_context,
                    source="auto_retry",
                )
            if interactive_result.action_progressed:
                watchdog_state.last_progress_event_ts = time.monotonic()
            if interactive_result.recorded_status:
                record_step_outcome(
                    step_outcomes=step_outcomes,
                    ui_findings=ui_findings,
                    idx=idx,
                    step=step,
                    status=interactive_result.recorded_status,
                )
            if post_action_pause_ms > 0:
                page.wait_for_timeout(post_action_pause_ms)
            if visual:
                ensure_visual_overlay_ready_best_effort(
                    page,
                    ui_findings,
                    cursor_expected=visual_cursor,
                    retries=3,
                    delay_ms=120,
                    debug_screenshot_path=overlay_debug_path,
                    force_reinit=True,
                )
            continue

        wait_result = execute_wait_step(
            page=page,
            step=step,
            idx=idx,
            total=total,
            teaching_mode=teaching_mode,
            step_hard_timeout_seconds=step_hard_timeout_seconds,
            run_deadline_ts=run_deadline_ts,
            wait_timeout_ms=wait_timeout_ms,
            watchdog_step_signature=watchdog_state.current_step_signature,
            observations=observations,
            ui_findings=ui_findings,
            console_errors=console_errors,
            remaining_ms=remaining_ms,
            trigger_timeout_handoff=trigger_timeout_handoff,
            force_main_frame_context=force_main_frame_context,
            apply_iframe_precheck_handoff=lambda **kwargs: apply_handoff_decision(
                evaluate_iframe_precheck_handoff(
                    page=page,
                    teaching_mode=teaching_mode,
                    attempted="main-frame-first precheck failed",
                    ui_findings=ui_findings,
                    show_custom_notice=show_custom_handoff_notice,
                    append_iframe_focus_findings=append_iframe_focus_findings,
                    control_enabled=run_state.control_enabled,
                    **kwargs,
                )
            ),
            apply_wait_step=lambda page_obj, step_obj, step_num, timeout_ms: apply_wait_step(
                page_obj,
                step_obj,
                step_num,
                actions,
                observations,
                ui_findings,
                timeout_ms=timeout_ms,
            ),
            add_timeout_evidence=lambda step_num: capture_timeout_evidence(
                page=page,
                evidence_dir=evidence_dir,
                evidence_paths=evidence_paths,
                name=f"step_{step_num}_timeout.png",
            ),
            runtime_closed=runtime_closed,
            session=session,
            is_page_closed_error=is_page_closed_error,
            is_timeout_error=is_timeout_error,
            should_soft_skip_wait_timeout=lambda **kwargs: should_soft_skip_wait_timeout(
                steps=steps, **kwargs
            ),
            append_wait_timeout_findings=append_wait_timeout_findings,
        )
        if wait_result.should_break:
            if wait_result.result == "failed":
                run_state.result = "failed"
                if wait_result.crashed:
                    append_run_crash_findings(ui_findings)
            break
        if wait_result.recorded_status:
            record_step_outcome(
                step_outcomes=step_outcomes,
                ui_findings=ui_findings,
                idx=idx,
                step=step,
                status=wait_result.recorded_status,
                reason=wait_result.recorded_reason,
            )
            if wait_result.recorded_status == "skipped_not_applicable":
                continue
        if visual:
            ensure_visual_overlay_ready_best_effort(
                page,
                ui_findings,
                cursor_expected=visual_cursor,
                retries=3,
                delay_ms=120,
                debug_screenshot_path=overlay_debug_path,
                force_reinit=True,
            )
        watchdog_state.last_progress_event_ts = time.monotonic()
        record_step_outcome(
            step_outcomes=step_outcomes,
            ui_findings=ui_findings,
            idx=idx,
            step=step,
            status="executed",
        )
        if teaching_mode and watchdog_stuck_attempt(attempted_hint or f"watchdog:post-step:{step.kind}"):
            break

    return StepLoopResult(step_outcomes=step_outcomes)
