"""Generic step applicability probes and timeout helpers."""

from __future__ import annotations

from typing import Any

from bridge.web_executor_steps import INTERACTIVE_STEP_KINDS
from bridge.web_steps import WebStep


def probe_step_target_state(page: Any, step: WebStep) -> dict[str, Any]:
    present: bool | None = None
    visible: bool | None = None
    enabled: bool | None = None
    try:
        if step.kind in {"click_text", "maybe_click_text"}:
            node = page.locator("body").get_by_text(step.target, exact=False).first
        else:
            node = page.locator(step.target).first
        try:
            present = bool(node.count() > 0)
        except Exception:
            present = None
        try:
            visible = bool(node.is_visible(timeout=180))
        except Exception:
            visible = None
        try:
            enabled = bool(node.is_enabled())
        except Exception:
            enabled = None
    except Exception:
        pass
    return {"present": present, "visible": visible, "enabled": enabled}


def interactive_step_not_applicable_reason(page: Any, step: WebStep) -> str:
    if step.kind not in INTERACTIVE_STEP_KINDS:
        return ""
    state = probe_step_target_state(page, step)
    if state.get("enabled") is False:
        return (
            "target disabled in current context "
            f"(present={state['present']}, visible={state['visible']}, enabled={state['enabled']})"
        )
    if (
        step.kind in {"click_text", "maybe_click_text"}
        and state.get("present") is False
        and state.get("visible") is False
    ):
        return (
            "target text not present/visible in current context "
            f"(present={state['present']}, visible={state['visible']}, enabled={state['enabled']})"
        )
    return ""


def is_timeout_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    if "timeout" in name:
        return True
    msg = str(exc).lower()
    return "timeout" in msg and "exceeded" in msg
