"""Unified live view for run status, observer events, and logs."""

from __future__ import annotations

import json
import time
from pathlib import Path

from bridge.storage import status_payload, tail_lines
from bridge.web_session import (
    get_last_session,
    load_and_refresh_session,
    refresh_session_state,
    request_session_state,
    session_agent_online,
    session_is_alive,
)


def _fmt_event(evt: dict[str, object]) -> str:
    sev = str(evt.get("severity", "info") or "info").lower()
    typ = str(evt.get("type", "") or "event")
    msg = str(evt.get("message", "") or "").strip()
    target = str(evt.get("target", "") or "").strip()
    if typ == "click":
        return f"[{sev}] click target=\"{target}\""
    if msg:
        return f"[{sev}] {msg}"
    return f"[{sev}] {typ}"


def _iter_log_lines(run_dir: Path, tail_count: int) -> list[str]:
    bridge_log = run_dir / "bridge.log"
    oi_stdout = run_dir / "oi_stdout.log"
    oi_stderr = run_dir / "oi_stderr.log"
    lines: list[str] = []
    lines.extend(tail_lines(bridge_log, tail_count))
    lines.extend(tail_lines(oi_stdout, tail_count))
    lines.extend(tail_lines(oi_stderr, tail_count))
    return [ln for ln in lines if ln.strip()]


def live_command(
    *,
    attach: str,
    interval_ms: int,
    tail: int,
    json_mode: bool,
) -> None:
    if interval_ms < 100:
        raise SystemExit("--interval-ms must be >= 100")

    if attach.strip().lower() == "last":
        session = get_last_session()
        if session is None:
            raise SystemExit("No last session available. Run web-open first.")
        session = refresh_session_state(session)
    else:
        session = load_and_refresh_session(attach)

    if not session_is_alive(session):
        raise SystemExit("Attached session is not alive; run web-open again.")

    seen_events: set[str] = set()
    seen_log_lines: set[str] = set()
    last_snapshot: tuple[str, str, str, str, str, bool, bool, bool] | None = None

    while True:
        try:
            payload = status_payload()
            session = refresh_session_state(session)
            observer = {}
            if session_agent_online(session):
                try:
                    observer = request_session_state(session)
                except SystemExit:
                    observer = {}
        except KeyboardInterrupt:
            return

        run_id = str(payload.get("run_id", ""))
        run_result = str(payload.get("result", ""))
        run_state = str(payload.get("state", ""))
        progress = str(payload.get("progress", ""))
        run_dir = Path(str(payload.get("run_dir", ""))) if payload.get("run_dir") else None

        event_lines: list[str] = []
        for evt in list(observer.get("recent_events", []) or []):
            if not isinstance(evt, dict):
                continue
            key = "|".join(
                [
                    str(evt.get("created_at", "")),
                    str(evt.get("type", "")),
                    str(evt.get("message", "")),
                    str(evt.get("status", "")),
                ]
            )
            if key in seen_events:
                continue
            seen_events.add(key)
            event_lines.append(_fmt_event(evt))

        log_lines: list[str] = []
        if run_dir is not None and run_dir.exists():
            for ln in _iter_log_lines(run_dir, tail):
                if ln in seen_log_lines:
                    continue
                seen_log_lines.add(ln)
                log_lines.append(ln)

        agent_online = session_agent_online(session)
        incident_open = bool(observer.get("incident_open", False))
        snapshot = (
            run_id,
            run_state,
            run_result,
            progress,
            session.state,
            bool(session.controlled),
            agent_online,
            incident_open,
        )

        # Quiet mode by default: print when state changed or new events/log lines arrived.
        if not event_lines and not log_lines and snapshot == last_snapshot:
            try:
                time.sleep(interval_ms / 1000.0)
            except KeyboardInterrupt:
                return
            continue

        output = {
            "run_id": run_id,
            "run_state": run_state,
            "run_result": run_result,
            "progress": progress,
            "session_id": session.session_id,
            "session_state": session.state,
            "controlled": session.controlled,
            "agent_online": agent_online,
            "incident_open": incident_open,
            "events": event_lines,
            "logs": log_lines,
        }

        if json_mode:
            print(json.dumps(output, ensure_ascii=False), flush=True)
        else:
            print(f"run={run_id} state={run_state} result={run_result} progress={progress}", flush=True)
            print(
                f"session={session.session_id} state={session.state} controlled={session.controlled} "
                f"agent_online={agent_online} "
                f"incident_open={incident_open}",
                flush=True,
            )
            for item in event_lines:
                print(f"event: {item}", flush=True)
            for item in log_lines:
                print(f"log: {item}", flush=True)
            print("---", flush=True)

        last_snapshot = snapshot

        try:
            time.sleep(interval_ms / 1000.0)
        except KeyboardInterrupt:
            return
