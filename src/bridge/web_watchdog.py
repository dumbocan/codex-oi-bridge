"""Watchdog state and evaluation helpers for web-run loops."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WebWatchdogConfig:
    stuck_iframe_seconds: float
    stuck_step_seconds: float
    stuck_interactive_seconds: float


@dataclass
class WebWatchdogState:
    current_step_signature: str = ""
    current_learning_target: str = ""
    last_step_change_ts: float = 0.0
    last_progress_event_ts: float = 0.0
    last_useful_events: int = 0


def update_step_signature(
    state: WebWatchdogState,
    *,
    step_signature: str,
    learning_target: str,
    now_ts: float,
) -> None:
    if step_signature != state.current_step_signature:
        state.current_step_signature = step_signature
        state.last_step_change_ts = now_ts
        state.last_progress_event_ts = now_ts
    state.current_learning_target = learning_target


def poll_progress(state: WebWatchdogState, *, useful_event_count: int, now_ts: float) -> None:
    if useful_event_count > state.last_useful_events:
        state.last_useful_events = useful_event_count
        state.last_progress_event_ts = now_ts


def evaluate_stuck_reason(
    state: WebWatchdogState,
    *,
    cfg: WebWatchdogConfig,
    now_ts: float,
    iframe_focus_locked: bool,
) -> str:
    sig = state.current_step_signature
    if not sig:
        return ""
    if (
        (now_ts - state.last_progress_event_ts) > max(0.1, cfg.stuck_iframe_seconds)
        and iframe_focus_locked
    ):
        return "stuck_iframe_focus"
    if (now_ts - state.last_step_change_ts) > max(0.1, cfg.stuck_step_seconds):
        return "stuck"
    if (now_ts - state.last_progress_event_ts) > max(0.1, cfg.stuck_interactive_seconds):
        return "stuck"
    return ""


def remaining_ms(deadline_ts: float, *, now_ts: float) -> int:
    return int(max(0.0, deadline_ts - now_ts) * 1000)
