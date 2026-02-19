"""Helpers for web executor step classification and repeated findings."""

from __future__ import annotations

INTERACTIVE_STEP_KINDS = {
    "click_selector",
    "click_text",
    "maybe_click_text",
    "bulk_click_in_cards",
    "bulk_click_until_empty",
    "fill_selector",
    "select_label",
    "select_value",
}

TEACHING_HANDOFF_KINDS = {
    "click_text",
    "click_selector",
    "bulk_click_in_cards",
    "bulk_click_until_empty",
    "fill_selector",
}

LEARNING_TARGET_STEP_KINDS = {
    "click_selector",
    "click_text",
    "maybe_click_text",
    "fill_selector",
    "select_label",
    "select_value",
}


def step_learning_target(step_kind: str, target: str) -> str:
    if step_kind in LEARNING_TARGET_STEP_KINDS:
        return str(target).strip()
    return ""


def append_run_crash_findings(ui_findings: list[str]) -> None:
    ui_findings.append("what_failed=run_crash")
    ui_findings.append("where=web-run")
    ui_findings.append("why_likely=page_or_context_closed")
    ui_findings.append("attempted=executor run")
    ui_findings.append("next_best_action=reopen session and retry")


def append_iframe_focus_findings(
    ui_findings: list[str], *, where: str, attempted: str, why_likely: str
) -> None:
    ui_findings.append("Me he quedado dentro de YouTube iframe. Te cedo el control.")
    ui_findings.append("what_failed=stuck_iframe_focus")
    ui_findings.append(f"where={where}")
    ui_findings.append(f"why_likely={why_likely}")
    ui_findings.append(f"attempted={attempted}")
    ui_findings.append("next_best_action=human_assist")


def append_interactive_timeout_findings(
    ui_findings: list[str],
    *,
    step_num: int,
    step_kind: str,
    step_target: str,
    timeout_ms: int,
) -> None:
    ui_findings.append(
        f"step {step_num} timeout on {step_kind}:{step_target} (timeout_ms={timeout_ms})"
    )
    ui_findings.append("what_failed=interactive_timeout")
    ui_findings.append(f"where=step {step_num}:{step_kind}:{step_target}")
    ui_findings.append(
        "why_likely=target unavailable/occluded or app did not become interactive in time"
    )
    ui_findings.append("attempted=interactive timeout path")
    ui_findings.append("next_best_action=inspect target visibility or use teaching handoff")


def append_wait_timeout_findings(
    ui_findings: list[str],
    *,
    step_num: int,
    step_kind: str,
    step_target: str,
    timeout_ms: int,
) -> None:
    ui_findings.append(
        f"step {step_num} timeout waiting for {step_kind}:{step_target} (timeout_ms={timeout_ms})"
    )
    ui_findings.append("what_failed=wait_timeout")
    ui_findings.append(f"where=step {step_num}:{step_kind}:{step_target}")
    ui_findings.append("why_likely=expected selector/text did not appear within timeout window")
    ui_findings.append("attempted=wait timeout path")
    ui_findings.append("next_best_action=verify app state or retry with stable selector")
