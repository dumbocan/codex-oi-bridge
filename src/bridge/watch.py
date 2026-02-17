"""Real-time watcher for web session observer state (/state polling)."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime

from bridge.web_session import (
    get_last_session,
    load_and_refresh_session,
    refresh_session_state,
    request_session_state,
    session_agent_online,
    session_is_alive,
)


def _severity_rank(severity: str) -> int:
    s = (severity or "").strip().lower()
    if s == "error":
        return 3
    if s == "warn":
        return 2
    return 1


def _min_rank_from_only(only: str) -> int:
    if only == "errors":
        return 3
    if only == "warn":
        return 2
    return 1


def _safe_time_hhmmss(iso_text: str) -> str:
    try:
        dt = datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except Exception:
        return "--:--:--"


def _format_event_line(evt: dict[str, object]) -> str:
    t = _safe_time_hhmmss(str(evt.get("created_at", "")))
    etype = str(evt.get("type", "") or "unknown").strip()
    sev = str(evt.get("severity", "") or "info").strip().lower()
    target = str(evt.get("target", "") or "").strip()
    selector = str(evt.get("selector", "") or "").strip()
    x = int(evt.get("x", 0) or 0)
    y = int(evt.get("y", 0) or 0)
    scroll_y = int(evt.get("scroll_y", 0) or 0)
    url = str(evt.get("url", "") or "").strip()
    msg = str(evt.get("message", "") or "").strip()
    status = int(evt.get("status", 0) or 0)

    if sev == "error":
        if msg:
            return f"{t} ERROR {msg}"
        if status:
            return f"{t} ERROR http {status} url={url}"
        return f"{t} ERROR {etype} url={url}"

    if sev == "warn":
        if msg:
            return f"{t} WARN {msg}"
        if status:
            return f"{t} WARN http {status} url={url}"
        return f"{t} WARN {etype} url={url}"

    if etype == "click":
        parts = [f"{t} click"]
        if target:
            parts.append(f'target="{target}"')
        if selector:
            parts.append(f"selector={selector}")
        if url:
            parts.append(f"url={url}")
        return " ".join(parts)
    if etype == "mousemove":
        return f"{t} mousemove x={x} y={y}"
    if etype == "scroll":
        return f"{t} scroll y={scroll_y}"

    if msg:
        return f"{t} {etype} {msg}"
    return f"{t} {etype} url={url}"


def _event_key(evt: dict[str, object]) -> str:
    return "|".join(
        [
            str(evt.get("created_at", "")),
            str(evt.get("type", "")),
            str(evt.get("message", "")),
            str(evt.get("status", "")),
        ]
    )


def _watch_loop(
    *,
    fetch_state,
    sleep_fn,
    interval_ms: int,
    since_last: bool,
    json_mode: bool,
    print_events: int,
    only: str,
    notify: bool,
) -> None:
    min_rank = _min_rank_from_only(only)
    seen: set[str] = set()
    cursor = ""
    prev_incident = None
    prev_ack_count = None

    first = True
    while True:
        state = fetch_state() or {}
        incident_open = bool(state.get("incident_open", False))
        last_error = str(state.get("last_error", "") or "")
        ack_count = int(state.get("ack_count", 0) or 0)
        events = list(state.get("recent_events", []) or [])

        if first:
            last_event_at = str(state.get("last_event_at", "") or "")
            if since_last and last_event_at:
                cursor = last_event_at
            if print_events and not since_last:
                tail = events[-int(print_events):]
                for evt in tail:
                    if not isinstance(evt, dict):
                        continue
                    sev = str(evt.get("severity", "") or "info")
                    if _severity_rank(sev) < min_rank:
                        continue
                    key = _event_key(evt)
                    seen.add(key)
                    if json_mode:
                        print(json.dumps({"type": "event", "event": evt}, ensure_ascii=False))
                    else:
                        print(_format_event_line(evt))
            prev_incident = incident_open
            prev_ack_count = ack_count
            first = False
        else:
            if prev_incident is not None and incident_open != prev_incident:
                if incident_open:
                    if notify:
                        sys.stdout.write("\a")
                        sys.stdout.flush()
                    if json_mode:
                        print(
                            json.dumps(
                                {
                                    "type": "incident_open",
                                    "last_error": last_error,
                                    "error_count": int(state.get("error_count", 0) or 0),
                                },
                                ensure_ascii=False,
                            )
                        )
                    else:
                        print(f"INCIDENT OPEN: {last_error}".rstrip())
                else:
                    if json_mode:
                        print(
                            json.dumps(
                                {"type": "incident_cleared", "ack_count": ack_count},
                                ensure_ascii=False,
                            )
                        )
                    else:
                        print(f"INCIDENT CLEARED (ack_count={ack_count})")

            if prev_ack_count is not None and ack_count > prev_ack_count:
                if json_mode:
                    print(json.dumps({"type": "ack", "ack_count": ack_count}, ensure_ascii=False))
                else:
                    print(f"ACK (ack_count={ack_count})")

            for evt in events:
                if not isinstance(evt, dict):
                    continue
                created_at = str(evt.get("created_at", "") or "")
                key = _event_key(evt)
                if key in seen:
                    continue
                if cursor and created_at and created_at <= cursor:
                    seen.add(key)
                    continue
                sev = str(evt.get("severity", "") or "info")
                if _severity_rank(sev) < min_rank:
                    seen.add(key)
                    continue
                seen.add(key)
                if created_at and created_at > cursor:
                    cursor = created_at
                if json_mode:
                    print(json.dumps({"type": "event", "event": evt}, ensure_ascii=False))
                else:
                    print(_format_event_line(evt))

            prev_incident = incident_open
            prev_ack_count = ack_count

        try:
            sleep_fn(max(50, int(interval_ms)) / 1000.0)
        except KeyboardInterrupt:
            return


def watch_command(
    *,
    attach: str,
    interval_ms: int,
    since_last: bool,
    json_mode: bool,
    print_events: int,
    only: str,
    notify: bool,
) -> None:
    if interval_ms < 50:
        raise SystemExit("--interval-ms must be >= 50")

    if attach.strip().lower() == "last":
        session = get_last_session()
        if session is None:
            raise SystemExit("No last session available. Run web-open first.")
        session = refresh_session_state(session)
    else:
        session = load_and_refresh_session(attach)

    if not session_is_alive(session):
        raise SystemExit("Attached session is not alive; run web-open again.")
    if not session_agent_online(session):
        raise SystemExit("Session control agent offline.")

    def fetch_state():
        return request_session_state(session)

    _watch_loop(
        fetch_state=fetch_state,
        sleep_fn=time.sleep,
        interval_ms=interval_ms,
        since_last=since_last,
        json_mode=json_mode,
        print_events=print_events,
        only=only,
        notify=notify,
    )
