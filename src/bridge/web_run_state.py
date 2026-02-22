"""Mutable runtime state helpers for web-run execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bridge.web_run_handoff import HandoffDecision


@dataclass
class WebRunMutableState:
    control_enabled: bool = False
    force_keep_open: bool = False
    release_for_handoff: bool = False
    wait_for_human_learning: bool = False
    handoff_reason: str = ""
    handoff_where: str = ""
    handoff_attempted: str = ""
    failed_target_for_teaching: str = ""
    result: str = ""


def apply_handoff_decision(state: WebRunMutableState, decision: HandoffDecision) -> bool:
    if not decision.triggered:
        return False
    state.handoff_reason = decision.handoff_reason
    state.handoff_where = decision.handoff_where
    state.handoff_attempted = decision.handoff_attempted
    state.failed_target_for_teaching = decision.failed_target_for_teaching
    state.force_keep_open = decision.force_keep_open
    state.wait_for_human_learning = decision.wait_for_human_learning
    state.release_for_handoff = decision.release_for_handoff
    if decision.control_enabled is not None:
        state.control_enabled = decision.control_enabled
    if decision.result:
        state.result = decision.result
    return True


def apply_handoff_updates(state: WebRunMutableState, updates: dict[str, Any]) -> bool:
    if not updates:
        return False
    state.force_keep_open = updates.get("force_keep_open", state.force_keep_open)
    state.wait_for_human_learning = updates.get(
        "wait_for_human_learning", state.wait_for_human_learning
    )
    state.release_for_handoff = updates.get("release_for_handoff", state.release_for_handoff)
    state.handoff_reason = updates.get("handoff_reason", state.handoff_reason)
    state.handoff_where = updates.get("handoff_where", state.handoff_where)
    state.handoff_attempted = updates.get("handoff_attempted", state.handoff_attempted)
    state.failed_target_for_teaching = updates.get(
        "failed_target_for_teaching", state.failed_target_for_teaching
    )
    state.control_enabled = updates.get("control_enabled", state.control_enabled)
    if updates.get("result"):
        state.result = updates["result"]
    return bool(updates.get("should_break"))
