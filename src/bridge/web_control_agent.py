"""Persistent control agent for session top-bar actions."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from bridge.web_session import (
    close_session,
    load_session,
    mark_controlled,
    refresh_session_state,
    session_is_alive,
)


def _session_payload(session: Any) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "state": session.state,
        "controlled": session.controlled,
        "url": session.url,
        "title": session.title,
        "last_seen_at": session.last_seen_at,
        "agent_online": True,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


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
    if action_name not in {"refresh", "release", "close"}:
        raise ValueError(f"Unsupported action: {action_name}")

    session = refresh_session_state(load_session(session_id))

    if action_name == "refresh":
        return _session_payload(session), False

    if action_name == "release":
        mark_controlled(session, False)
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
    server_version = "BridgeControlAgent/1.0"

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
        if self.path != "/health":
            self._send_json(404, {"error": "not_found"})
            return
        self._send_json(200, {"ok": True, "session_id": self.server.session_id})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/action":
            self._send_json(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid_json"})
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
