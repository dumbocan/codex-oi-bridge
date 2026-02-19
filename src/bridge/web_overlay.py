"""Overlay and top-bar helpers for web visual control modes."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from bridge.web_session import WebSession


def _page_is_closed(page: Any | None) -> bool:
    if page is None:
        return True
    checker = getattr(page, "is_closed", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return True
    return False


def _observer_noise_mode() -> str:
    raw = str(os.getenv("BRIDGE_OBSERVER_NOISE_MODE", "minimal")).strip().lower()
    return "debug" if raw == "debug" else "minimal"


def set_assistant_control_overlay(page: Any, enabled: bool) -> None:
    if _page_is_closed(page):
        return
    try:
        page.evaluate(
            """
        ([enabled]) => {
          const id = '__bridge_assistant_control_overlay';
          const existing = document.getElementById(id);
          if (!enabled) {
            if (existing) existing.remove();
            return;
          }
          if (existing) return;
          const wrap = document.createElement('div');
          wrap.id = id;
          wrap.style.position = 'fixed';
          wrap.style.inset = '0';
          wrap.style.border = '3px solid #3BA7FF';
          wrap.style.boxSizing = 'border-box';
          wrap.style.pointerEvents = 'none';
          wrap.style.zIndex = '2147483645';
          const badge = document.createElement('div');
          badge.textContent = 'ASSISTANT CONTROL';
          badge.style.position = 'fixed';
          badge.style.top = '10px';
          badge.style.right = '12px';
          badge.style.padding = '4px 8px';
          badge.style.borderRadius = '999px';
          badge.style.font = '11px/1.2 monospace';
          badge.style.color = '#fff';
          badge.style.background = 'rgba(59,167,255,0.9)';
          badge.style.pointerEvents = 'none';
          wrap.appendChild(badge);
          document.documentElement.appendChild(wrap);
        }
            """,
            [enabled],
        )
    except Exception:
        return


def set_user_control_overlay(page: Any, enabled: bool) -> None:
    if _page_is_closed(page):
        return
    try:
        page.evaluate(
            """
        ([enabled]) => {
          const id = '__bridge_user_control_overlay';
          const existing = document.getElementById(id);
          if (!enabled) {
            if (existing) existing.remove();
            return;
          }
          if (existing) return;
          const wrap = document.createElement('div');
          wrap.id = id;
          wrap.style.position = 'fixed';
          wrap.style.inset = '0';
          wrap.style.border = '3px solid #22c55e';
          wrap.style.boxSizing = 'border-box';
          wrap.style.pointerEvents = 'none';
          wrap.style.zIndex = '2147483644';
          const badge = document.createElement('div');
          badge.textContent = 'USER CONTROL';
          badge.style.position = 'fixed';
          badge.style.top = '10px';
          badge.style.right = '12px';
          badge.style.padding = '4px 8px';
          badge.style.borderRadius = '999px';
          badge.style.font = '11px/1.2 monospace';
          badge.style.color = '#fff';
          badge.style.background = 'rgba(34,197,94,0.9)';
          badge.style.pointerEvents = 'none';
          wrap.appendChild(badge);
          document.documentElement.appendChild(wrap);
        }
            """,
            [enabled],
        )
    except Exception:
        return


def set_learning_handoff_overlay(page: Any, enabled: bool) -> None:
    if _page_is_closed(page):
        return
    try:
        page.evaluate(
            """
        ([enabled]) => {
          const id = '__bridge_learning_handoff_overlay';
          const existing = document.getElementById(id);
          if (!enabled) {
            if (existing) existing.remove();
            return;
          }
          if (existing) return;
          const wrap = document.createElement('div');
          wrap.id = id;
          wrap.style.position = 'fixed';
          wrap.style.inset = '0';
          wrap.style.border = '3px solid #f59e0b';
          wrap.style.boxSizing = 'border-box';
          wrap.style.pointerEvents = 'none';
          wrap.style.zIndex = '2147483645';
          const badge = document.createElement('div');
          badge.textContent = 'LEARNING/HANDOFF';
          badge.style.position = 'fixed';
          badge.style.top = '10px';
          badge.style.right = '12px';
          badge.style.padding = '4px 8px';
          badge.style.borderRadius = '999px';
          badge.style.font = '11px/1.2 monospace';
          badge.style.color = '#111';
          badge.style.background = 'rgba(245,158,11,0.95)';
          badge.style.pointerEvents = 'none';
          wrap.appendChild(badge);
          document.documentElement.appendChild(wrap);
        }
            """,
            [enabled],
        )
    except Exception:
        return


def session_state_payload(
    session: WebSession | None,
    *,
    override_controlled: bool | None = None,
    override_state: str | None = None,
    learning_active: bool = False,
) -> dict[str, Any]:
    if session is None:
        return {}
    control_port = int(session.control_port or 0)
    return {
        "session_id": session.session_id,
        "url": session.url,
        "title": session.title,
        "controlled": session.controlled if override_controlled is None else override_controlled,
        "state": session.state if override_state is None else override_state,
        "learning_active": bool(learning_active),
        "observer_noise_mode": _observer_noise_mode(),
        "last_seen_at": session.last_seen_at,
        "control_port": control_port,
        "control_url": f"http://127.0.0.1:{control_port}" if control_port > 0 else "",
        "agent_online": control_port > 0,
    }


def update_top_bar_state(page: Any, payload: dict[str, Any]) -> None:
    if _page_is_closed(page):
        return
    try:
        page.evaluate("([payload]) => window.__bridgeUpdateTopBarState?.(payload)", [payload])
    except Exception:
        return


def destroy_top_bar(page: Any) -> None:
    if _page_is_closed(page):
        return
    try:
        page.evaluate("() => window.__bridgeDestroyTopBar?.()")
    except Exception:
        return


def notify_learning_state(session: WebSession | None, *, active: bool, window_seconds: int) -> None:
    if session is None:
        return
    port = int(session.control_port or 0)
    if port <= 0:
        return
    payload = {
        "type": "learning_on" if active else "learning_off",
        "window_seconds": int(max(1, min(600, window_seconds))),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/event",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0):
            return
    except Exception:
        return
