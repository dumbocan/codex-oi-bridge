"""Handoff UI/state helpers for teaching mode."""

from __future__ import annotations

from typing import Any, Callable


def show_stuck_handoff_notice(page: Any, step_text: str) -> None:
    msg = f"Me he atascado en: {step_text}. Te cedo el control para que me ayudes."
    try:
        page.evaluate(
            """
            ([message]) => {
              const id = '__bridge_teaching_handoff_notice';
              let el = document.getElementById(id);
              if (!el) {
                el = document.createElement('div');
                el.id = id;
                el.style.position = 'fixed';
                el.style.left = '50%';
                el.style.bottom = '18px';
                el.style.transform = 'translateX(-50%)';
                el.style.padding = '10px 14px';
                el.style.borderRadius = '10px';
                el.style.background = 'rgba(245,158,11,0.95)';
                el.style.color = '#fff';
                el.style.font = '13px/1.3 monospace';
                el.style.zIndex = '2147483647';
                el.style.boxShadow = '0 8px 18px rgba(0,0,0,0.3)';
                document.documentElement.appendChild(el);
              }
              el.textContent = String(message || '');
            }
            """,
            [msg],
        )
    except Exception:
        return


def show_custom_handoff_notice(page: Any, message: str) -> None:
    try:
        page.evaluate(
            """
            ([msg]) => {
              const id = '__bridge_teaching_handoff_notice';
              let el = document.getElementById(id);
              if (!el) {
                el = document.createElement('div');
                el.id = id;
                el.style.position = 'fixed';
                el.style.left = '50%';
                el.style.bottom = '18px';
                el.style.transform = 'translateX(-50%)';
                el.style.padding = '10px 14px';
                el.style.borderRadius = '10px';
                el.style.background = 'rgba(245,158,11,0.95)';
                el.style.color = '#fff';
                el.style.font = '13px/1.3 monospace';
                el.style.zIndex = '2147483647';
                el.style.boxShadow = '0 8px 18px rgba(0,0,0,0.3)';
                document.documentElement.appendChild(el);
              }
              el.textContent = String(msg || '');
            }
            """,
            [message],
        )
    except Exception:
        return


def trigger_stuck_handoff(
    *,
    page: Any,
    session: Any | None,
    visual: bool,
    control_enabled: bool,
    where: str,
    attempted: str,
    learning_window_seconds: int,
    actions: list[str],
    ui_findings: list[str],
    what_failed: str = "stuck",
    notice_message: str = "",
    why_likely: str = "step unchanged/no useful progress within stuck thresholds during teaching mode",
    show_custom_notice: Callable[[Any, str], None],
    show_stuck_notice: Callable[[Any, str], None],
    set_learning_handoff_overlay: Callable[[Any, bool], None],
    set_assistant_control_overlay: Callable[[Any, bool], None],
    mark_controlled: Callable[[Any, bool], None],
    safe_page_title: Callable[[Any], str],
    notify_learning_state: Callable[[Any, bool, int], None],
    update_top_bar_state: Callable[[Any, dict[str, Any]], None],
    session_state_payload: Callable[..., dict[str, Any]],
) -> bool:
    if notice_message:
        show_custom_notice(page, notice_message)
    else:
        show_stuck_notice(page, where)
    set_learning_handoff_overlay(page, True)
    if visual and control_enabled:
        set_assistant_control_overlay(page, False)
    if session is not None:
        mark_controlled(session, False, url=getattr(page, "url", ""), title=safe_page_title(page))
        notify_learning_state(session, active=True, window_seconds=learning_window_seconds)
        try:
            update_top_bar_state(
                page,
                session_state_payload(session, override_controlled=False, learning_active=True),
            )
        except Exception:
            pass
    if "cmd: playwright release control (teaching handoff)" not in actions:
        actions.append("cmd: playwright release control (teaching handoff)")
    ui_findings.append(
        notice_message or f"Me he atascado en: {where}. Te cedo el control para que me ayudes."
    )
    if "control released" not in ui_findings:
        ui_findings.append("control released")
    ui_findings.append(f"what_failed={what_failed}")
    ui_findings.append(f"where={where}")
    ui_findings.append(f"attempted={attempted or 'watchdog'}")
    ui_findings.append("next_best_action=human_assist")
    ui_findings.append(f"why_likely={why_likely}")
    return False
