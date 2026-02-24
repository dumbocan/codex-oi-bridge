"""Handoff helpers that return state updates for stuck/target failures."""

from __future__ import annotations

from typing import Any, Callable


def retry_stuck_handoff(
    *,
    step_signature: str,
    attempted: str,
    target: str,
    session: Any | None,
    page: Any,
    visual: bool,
    learning_window_seconds: int,
    actions: list[str],
    ui_findings: list[str],
    trigger_stuck_handoff: Callable[..., bool],
    control_enabled: bool,
) -> dict[str, Any]:
    updated_control = trigger_stuck_handoff(
        page=page,
        session=session,
        visual=visual,
        control_enabled=control_enabled,
        where=step_signature,
        attempted=attempted,
        learning_window_seconds=learning_window_seconds,
        actions=actions,
        ui_findings=ui_findings,
    )
    return {
        "force_keep_open": True,
        "wait_for_human_learning": True,
        "release_for_handoff": False,
        "handoff_reason": "stuck",
        "handoff_where": step_signature,
        "handoff_attempted": attempted,
        "failed_target_for_teaching": target,
        "control_enabled": updated_control,
        "result": "partial",
        "should_break": True,
    }


def target_not_found_handoff(
    *,
    teaching_mode: bool,
    step_kind: str,
    step_target: str,
    interactive_step: int,
    learning_notes: list[str],
    ui_findings: list[str],
    page: Any,
    show_teaching_notice: Callable[[Any, str], None],
    failure_message: str = "",
) -> dict[str, Any]:
    if not (teaching_mode and step_kind in {"click_selector", "click_text"}):
        return {}
    learning_notes.append(f"failed target: {step_target}")
    failure_message_n = str(failure_message or "").strip().lower()
    is_no_effect_click = "bulk click in cards found no matching clickable targets" in failure_message_n

    ui_findings.append(f"No encuentro el botón: {step_target}. Te cedo el control.")
    ui_findings.append(f"what_failed={'no_effect_click' if is_no_effect_click else 'target_not_found'}")
    ui_findings.append(f"where=step {interactive_step}:{step_kind}:{step_target}")
    if is_no_effect_click:
        ui_findings.append(
            "why_likely=no matching visible clickable targets found after card scan/scroll retries"
        )
        ui_findings.append("attempted=card scan + container/page scroll retries")
    else:
        ui_findings.append(
            "why_likely=target text/selector changed, hidden, or not yet rendered"
        )
        ui_findings.append("attempted=stable selector candidates + container/page scroll retries")
    ui_findings.append("next_best_action=human_assist")
    show_teaching_notice(page, step_target)
    return {
        "force_keep_open": True,
        "release_for_handoff": True,
        "wait_for_human_learning": True,
        "failed_target_for_teaching": step_target,
        "result": "partial",
        "should_break": True,
    }
