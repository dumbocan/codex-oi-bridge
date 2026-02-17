"""Persistent control agent for session top-bar actions and live observation."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any

from bridge.web_session import (
    close_session,
    load_session,
    mark_controlled,
    refresh_session_state,
    session_is_alive,
)


class _AgentRuntime:
    def __init__(self) -> None:
        self._lock = Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=120)
        self._incident_open = False
        self._last_error = ""
        self._error_count = 0
        self._ack_count = 0
        self._last_ack_at = ""
        self._last_ack_by = ""
        self._learning_active_until = 0.0

    def record_event(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("type", "")).strip().lower() or "unknown"
        if event_type == "learning_on":
            seconds = float(payload.get("window_seconds", 25) or 25)
            self.set_learning_active(seconds=max(1.0, min(600.0, seconds)))
            return
        if event_type == "learning_off":
            self.set_learning_inactive()
            return
        message = str(payload.get("message", ""))[:400]
        status = int(payload.get("status", 0) or 0)
        noise_mode = _observer_noise_mode()
        controlled = bool(payload.get("controlled", False))
        learning_active = bool(payload.get("learning_active", False)) or self._learning_active()
        if noise_mode == "minimal" and not controlled and not learning_active and event_type in {
            "click",
            "mousemove",
            "scroll",
        }:
            return
        severity = self._event_severity(event_type, status, message)
        now = datetime.now(timezone.utc).isoformat()
        event = {
            "type": event_type,
            "severity": severity,
            "message": message,
            "url": str(payload.get("url", ""))[:300],
            "status": status,
            "target": str(payload.get("target", ""))[:180],
            "selector": str(payload.get("selector", ""))[:240],
            "text": str(payload.get("text", ""))[:240],
            "x": int(payload.get("x", 0) or 0),
            "y": int(payload.get("y", 0) or 0),
            "scroll_y": int(payload.get("scroll_y", 0) or 0),
            "created_at": now,
        }
        with self._lock:
            self._events.append(event)
            if severity == "error":
                self._incident_open = True
                self._error_count += 1
                reason = event["message"] or event["url"] or event_type
                self._last_error = reason[:220]

    def set_learning_active(self, seconds: float) -> None:
        with self._lock:
            self._learning_active_until = time.time() + float(seconds)

    def set_learning_inactive(self) -> None:
        with self._lock:
            self._learning_active_until = 0.0

    def _learning_active(self) -> bool:
        with self._lock:
            return time.time() < float(self._learning_active_until or 0.0)

    @staticmethod
    def _event_severity(event_type: str, status: int, message: str) -> str:
        low = message.lower().strip()
        if event_type == "click":
            return "info"
        if event_type in {"mousemove", "scroll"}:
            return "info"
        if event_type in {"network_warn", "console_warn"}:
            return "warn"
        if event_type == "network_error":
            # 4xx are usually user/input/auth flow noise; 5xx/0 are service/runtime failures.
            return "error" if status == 0 or status >= 500 else "warn"
        if event_type in {"console_error", "page_error"}:
            if "resizeobserver loop limit exceeded" in low:
                return "warn"
            if "favicon.ico" in low and "404" in low:
                return "warn"
            return "error"
        return "warn"

    def acknowledge_incident(self, actor: str = "operator") -> None:
        with self._lock:
            self._incident_open = False
            self._last_error = ""
            self._ack_count += 1
            self._last_ack_at = datetime.now(timezone.utc).isoformat()
            self._last_ack_by = actor[:40]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            learning_active = self._learning_active_until > 0.0 and (
                time.time() < float(self._learning_active_until)
            )
            recent = list(self._events)[-12:]
            last_event_at = recent[-1]["created_at"] if recent else ""
            return {
                "incident_open": self._incident_open,
                "last_error": self._last_error,
                "error_count": self._error_count,
                "ack_count": self._ack_count,
                "last_ack_at": self._last_ack_at,
                "last_ack_by": self._last_ack_by,
                "learning_active": learning_active,
                "observer_noise_mode": _observer_noise_mode(),
                "last_event_at": last_event_at,
                "recent_events": recent,
            }


_RUNTIME = _AgentRuntime()


def _observer_noise_mode() -> str:
    raw = str(os.getenv("BRIDGE_OBSERVER_NOISE_MODE", "minimal")).strip().lower()
    return "debug" if raw == "debug" else "minimal"


def _session_payload(session: Any) -> dict[str, Any]:
    payload = {
        "session_id": session.session_id,
        "state": session.state,
        "controlled": session.controlled,
        "url": session.url,
        "title": session.title,
        "last_seen_at": session.last_seen_at,
        "agent_online": True,
        "control_port": int(getattr(session, "control_port", 0) or 0),
        "control_url": (
            f"http://127.0.0.1:{int(getattr(session, 'control_port', 0) or 0)}"
            if int(getattr(session, "control_port", 0) or 0) > 0
            else ""
        ),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(_RUNTIME.snapshot())
    return payload


def _update_overlay_state(session: Any, *, controlled: bool | None = None, destroy_top_bar: bool = False) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return

    if not session_is_alive(session):
        return

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{session.port}")
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            return
        page = context.pages[0] if context.pages else None
        if page is None:
            return
        if controlled is not None:
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
                  document.documentElement.appendChild(wrap);
                }
                """,
                [controlled],
            )
        if destroy_top_bar:
            page.evaluate("() => window.__bridgeDestroyTopBar?.()")
        else:
            page.evaluate(
                "([payload]) => window.__bridgeUpdateTopBarState?.(payload)",
                [_session_payload(session)],
            )


def perform_session_action(session_id: str, action: str) -> tuple[dict[str, Any], bool]:
    action_name = str(action or "").strip().lower()
    if action_name not in {"refresh", "release", "close", "ack"}:
        raise ValueError(f"Unsupported action: {action_name}")

    session = refresh_session_state(load_session(session_id))

    if action_name == "refresh":
        return _session_payload(session), False

    if action_name == "ack":
        _RUNTIME.acknowledge_incident(actor="operator")
        payload = _session_payload(session)
        payload["message"] = "incident acknowledged"
        return payload, False

    if action_name == "release":
        mark_controlled(session, False)
        _RUNTIME.set_learning_inactive()
        session = refresh_session_state(load_session(session_id))
        _update_overlay_state(session, controlled=False)
        payload = _session_payload(session)
        payload["message"] = "control released"
        return payload, False

    # close
    _update_overlay_state(session, controlled=False, destroy_top_bar=True)
    close_session(session)
    closed = refresh_session_state(load_session(session_id))
    payload = _session_payload(closed)
    payload["state"] = "closed"
    payload["controlled"] = False
    payload["message"] = "session closed"
    return payload, True


class _ControlHandler(BaseHTTPRequestHandler):
    server_version = "BridgeControlAgent/1.1"

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json(200, {"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"ok": True, "session_id": self.server.session_id})
            return
        if self.path == "/state":
            try:
                session = refresh_session_state(load_session(self.server.session_id))
            except Exception as exc:  # pragma: no cover
                self._send_json(409, {"error": str(exc)})
                return
            self._send_json(200, _session_payload(session))
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid_json"})
            return

        if self.path == "/event":
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "invalid_event_payload"})
                return
            _RUNTIME.record_event(payload)
            self._send_json(200, {"ok": True})
            return

        if self.path != "/action":
            self._send_json(404, {"error": "not_found"})
            return

        try:
            result, should_shutdown = perform_session_action(
                self.server.session_id,
                str(payload.get("action", "")),
            )
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except SystemExit as exc:
            self._send_json(409, {"error": str(exc)})
            return
        except Exception as exc:  # pragma: no cover
            self._send_json(500, {"error": str(exc)})
            return

        self._send_json(200, result)
        if should_shutdown:
            self.server.should_shutdown = True

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class _ControlServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], session_id: str):
        super().__init__(server_address, _ControlHandler)
        self.session_id = session_id
        self.should_shutdown = False


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bridge.web_control_agent")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()

    server = _ControlServer(("127.0.0.1", args.port), args.session_id)
    try:
        while True:
            server.handle_request()
            if server.should_shutdown:
                break
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
