"""Helpers to evaluate watchdog/timeout handoff decisions during web runs."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from bridge.web_watchdog import (
    WebWatchdogConfig,
    WebWatchdogState,
    evaluate_stuck_reason,
    poll_progress as watchdog_poll_progress,
)


@dataclass(frozen=True)
class HandoffDecision:
    triggered: bool = False
    handoff_reason: str = ""
    handoff_where: str = ""
    handoff_attempted: str = ""
    failed_target_for_teaching: str = ""
    force_keep_open: bool = False
    wait_for_human_learning: bool = False
    release_for_handoff: bool = False
    result: str = ""
    control_enabled: bool | None = None


def evaluate_watchdog_handoff(
    *,
    page: Any,
    session: Any | None,
    watchdog_state: WebWatchdogState,
    watchdog_cfg: WebWatchdogConfig,
    attempted: str,
    teaching_mode: bool,
    visual: bool,
    control_enabled: bool,
    learning_window_seconds: int,
    ui_findings: list[str],
    actions: list[str],
    observer_useful_event_count: Callable[[Any | None], int],
    is_iframe_focus_locked: Callable[[Any], bool],
    show_custom_notice: Callable[[Any, str], None],
    trigger_stuck_handoff: Callable[..., bool],
) -> HandoffDecision:
    now = time.monotonic()
    useful = observer_useful_event_count(session)
    watchdog_poll_progress(watchdog_state, useful_event_count=useful, now_ts=now)
    stuck_reason = evaluate_stuck_reason(
        watchdog_state,
        cfg=watchdog_cfg,
        now_ts=now,
        iframe_focus_locked=is_iframe_focus_locked(page),
    )
    if stuck_reason == "stuck_iframe_focus":
        handoff_where = watchdog_state.current_step_signature
        handoff_attempted = f"{attempted}, iframe_focus>{watchdog_cfg.stuck_iframe_seconds}s"
        show_custom_notice(page, "Me he quedado dentro de YouTube iframe. Te cedo el control.")
        ui_findings.append("Me he quedado dentro de YouTube iframe. Te cedo el control.")
        ui_findings.append("what_failed=stuck_iframe_focus")
        ui_findings.append(f"where={handoff_where}")
        ui_findings.append("why_likely=focus/cursor remained in iframe without useful progress")
        ui_findings.append(f"attempted={handoff_attempted}")
        ui_findings.append("next_best_action=human_assist")
        return HandoffDecision(
            triggered=True,
            handoff_reason="stuck_iframe_focus",
            handoff_where=handoff_where,
            handoff_attempted=handoff_attempted,
            failed_target_for_teaching=watchdog_state.current_learning_target,
            force_keep_open=True,
            wait_for_human_learning=False,
            release_for_handoff=True,
            result="partial",
            control_enabled=control_enabled,
        )
    if stuck_reason != "stuck":
        return HandoffDecision()
    if not teaching_mode:
        return HandoffDecision(
            triggered=True,
            handoff_reason="stuck",
            handoff_where=watchdog_state.current_step_signature,
            handoff_attempted=attempted,
            failed_target_for_teaching=watchdog_state.current_learning_target,
            force_keep_open=True,
            wait_for_human_learning=False,
            release_for_handoff=False,
            result="failed",
            control_enabled=control_enabled,
        )
    updated_control = trigger_stuck_handoff(
        page=page,
        session=session,
        visual=visual,
        control_enabled=control_enabled,
        where=watchdog_state.current_step_signature,
        attempted=attempted,
        learning_window_seconds=learning_window_seconds,
        actions=actions,
        ui_findings=ui_findings,
    )
    return HandoffDecision(
        triggered=True,
        handoff_reason="stuck",
        handoff_where=watchdog_state.current_step_signature,
        handoff_attempted=attempted,
        failed_target_for_teaching=watchdog_state.current_learning_target,
        force_keep_open=True,
        wait_for_human_learning=True,
        release_for_handoff=False,
        result="partial",
        control_enabled=updated_control,
    )


def evaluate_timeout_handoff(
    *,
    page: Any,
    session: Any | None,
    what_failed: str,
    where: str,
    learning_target: str,
    attempted: str,
    why_likely: str,
    notice_message: str,
    teaching_mode: bool,
    visual: bool,
    control_enabled: bool,
    learning_window_seconds: int,
    ui_findings: list[str],
    actions: list[str],
    is_learning_target_candidate: Callable[[str], bool],
    trigger_stuck_handoff: Callable[..., bool],
) -> HandoffDecision:
    failed_target = learning_target if is_learning_target_candidate(learning_target) else ""
    if teaching_mode:
        updated_control = trigger_stuck_handoff(
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
        )
        return HandoffDecision(
            triggered=True,
            handoff_reason=what_failed,
            handoff_where=where,
            handoff_attempted=attempted,
            failed_target_for_teaching=failed_target,
            force_keep_open=True,
            wait_for_human_learning=True,
            release_for_handoff=False,
            result="partial",
            control_enabled=updated_control,
        )
    ui_findings.append(f"what_failed={what_failed}")
    ui_findings.append(f"where={where}")
    ui_findings.append(f"why_likely={why_likely}")
    ui_findings.append(f"attempted={attempted}")
    ui_findings.append("next_best_action=inspect logs and retry")
    return HandoffDecision(
        triggered=True,
        handoff_reason=what_failed,
        handoff_where=where,
        handoff_attempted=attempted,
        failed_target_for_teaching=failed_target,
        force_keep_open=True,
        wait_for_human_learning=True,
        release_for_handoff=False,
        result="failed",
        control_enabled=control_enabled,
    )


def evaluate_iframe_precheck_handoff(
    *,
    page: Any,
    teaching_mode: bool,
    where: str,
    learning_target: str,
    attempted: str,
    why_likely: str,
    ui_findings: list[str],
    show_custom_notice: Callable[[Any, str], None],
    append_iframe_focus_findings: Callable[..., None],
    control_enabled: bool,
) -> HandoffDecision:
    if not teaching_mode:
        return HandoffDecision()
    show_custom_notice(page, "Me he quedado dentro de YouTube iframe. Te cedo el control.")
    append_iframe_focus_findings(
        ui_findings,
        where=where,
        attempted=attempted,
        why_likely=why_likely,
    )
    return HandoffDecision(
        triggered=True,
        handoff_reason="stuck_iframe_focus",
        handoff_where=where,
        handoff_attempted=attempted,
        failed_target_for_teaching=learning_target,
        force_keep_open=True,
        wait_for_human_learning=False,
        release_for_handoff=True,
        result="partial",
        control_enabled=control_enabled,
    )
