"""Persistent browser session lifecycle for web mode."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUNS_DIR = Path("runs")
SESSIONS_DIR = RUNS_DIR / "web_sessions"
INDEX_PATH = SESSIONS_DIR / "index.json"


@dataclass
class WebSession:
    session_id: str
    pid: int
    port: int
    user_data_dir: str
    browser_binary: str
    url: str
    title: str
    controlled: bool
    created_at: str
    last_seen_at: str
    state: str = "open"
    control_port: int = 0
    agent_pid: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_session(initial_url: str | None = None) -> WebSession:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = SESSIONS_DIR / session_id
    attempt = 0
    while base.exists():
        attempt += 1
        base = SESSIONS_DIR / f"{session_id}-{attempt:02d}"
    base.mkdir(parents=True, exist_ok=False)

    browser = _find_browser_binary()
    port = _get_free_port()
    user_data_dir = base / "user-data"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    out_log = base / "browser_stdout.log"
    err_log = base / "browser_stderr.log"
    start_url = initial_url or "about:blank"

    cmd = [
        browser,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--new-window",
        start_url,
        "--no-first-run",
        "--no-default-browser-check",
    ]
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "start_new_session": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        )

    with out_log.open("w", encoding="utf-8") as out_fh, err_log.open("w", encoding="utf-8") as err_fh:
        proc = subprocess.Popen(cmd, stdout=out_fh, stderr=err_fh, **popen_kwargs)

    _wait_for_cdp(port, timeout_seconds=15)

    now = datetime.now(timezone.utc).isoformat()
    session = WebSession(
        session_id=base.name,
        pid=proc.pid,
        port=port,
        user_data_dir=str(user_data_dir),
        browser_binary=browser,
        url=start_url,
        title="",
        controlled=False,
        created_at=now,
        last_seen_at=now,
        state="open",
    )
    _ensure_control_agent(session)
    save_session(session)
    set_last_session_id(session.session_id)
    return session


def save_session(session: WebSession) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = SESSIONS_DIR / f"{session.session_id}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(session.to_dict(), fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def load_session(session_id: str) -> WebSession:
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        raise SystemExit(f"Unknown session_id: {session_id}")
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    payload.setdefault("control_port", 0)
    payload.setdefault("agent_pid", 0)
    return WebSession(**payload)


def load_and_refresh_session(session_id: str) -> WebSession:
    return refresh_session_state(load_session(session_id))


def set_last_session_id(session_id: str) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    with INDEX_PATH.open("w", encoding="utf-8") as fh:
        json.dump({"last_session_id": session_id}, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def get_last_session() -> WebSession | None:
    if not INDEX_PATH.exists():
        return None
    with INDEX_PATH.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    session_id = payload.get("last_session_id")
    if not session_id:
        return None
    try:
        return load_and_refresh_session(session_id)
    except SystemExit:
        return None


def session_is_alive(session: WebSession) -> bool:
    return _pid_alive(session.pid) and _cdp_alive(session.port)


def session_agent_online(session: WebSession) -> bool:
    if session.agent_pid <= 0 or session.control_port <= 0:
        return False
    if not _pid_alive(session.agent_pid):
        return False
    return _agent_ping(session.control_port)


def request_session_action(session: WebSession, action: str, timeout_seconds: float = 4.0) -> dict[str, Any]:
    port = int(session.control_port or 0)
    if port <= 0:
        raise SystemExit("Session control agent offline: no control port configured.")
    if not session_agent_online(session):
        raise SystemExit("Session control agent offline.")
    payload = json.dumps({"action": action}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/action",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        reason = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Session control action failed ({action}): {reason}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"Session control action failed ({action}): {exc}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Session control action returned invalid JSON ({action})") from exc
    if not isinstance(parsed, dict):
        raise SystemExit(f"Session control action returned invalid payload ({action})")
    return parsed


def refresh_session_state(session: WebSession) -> WebSession:
    alive = session_is_alive(session)

    if alive:
        if session.state != "open":
            session.state = "open"
        target = _cdp_primary_target(session.port)
        if target is not None:
            target_url = str(target.get("url", "")).strip()
            target_title = str(target.get("title", "")).strip()
            if target_url:
                session.url = target_url
            session.title = target_title
    else:
        session.state = "closed"
        session.controlled = False

    if alive:
        _ensure_control_agent(session)
    else:
        session.agent_pid = 0
        session.control_port = 0

    session.last_seen_at = datetime.now(timezone.utc).isoformat()
    save_session(session)
    return session


def mark_controlled(session: WebSession, controlled: bool, url: str | None = None, title: str | None = None) -> None:
    session = refresh_session_state(session)
    session.controlled = controlled and session.state == "open"
    if url is not None:
        session.url = url
    if title is not None:
        session.title = title
    session.last_seen_at = datetime.now(timezone.utc).isoformat()
    save_session(session)


def close_session(session: WebSession) -> None:
    session = refresh_session_state(session)
    if _pid_alive(session.pid):
        try:
            os.kill(session.pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):
            if not _pid_alive(session.pid):
                break
            time.sleep(0.1)
        if _pid_alive(session.pid):
            try:
                os.kill(session.pid, signal.SIGKILL)
            except OSError:
                pass
    _stop_control_agent(session)
    session.controlled = False
    session.state = "closed"
    session.agent_pid = 0
    session.control_port = 0
    session.last_seen_at = datetime.now(timezone.utc).isoformat()
    save_session(session)


def _ensure_control_agent(session: WebSession) -> None:
    if session.control_port > 0 and session_agent_online(session):
        return

    control_port = _get_free_port()
    session_dir = Path(session.user_data_dir).parent
    out_log = session_dir / "agent_stdout.log"
    err_log = session_dir / "agent_stderr.log"

    cmd = [
        sys.executable,
        "-m",
        "bridge.web_control_agent",
        "--session-id",
        session.session_id,
        "--port",
        str(control_port),
    ]
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "start_new_session": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        )

    with out_log.open("a", encoding="utf-8") as out_fh, err_log.open("a", encoding="utf-8") as err_fh:
        proc = subprocess.Popen(cmd, stdout=out_fh, stderr=err_fh, **popen_kwargs)

    _wait_for_agent(control_port, timeout_seconds=8)
    session.control_port = control_port
    session.agent_pid = proc.pid


def _stop_control_agent(session: WebSession) -> None:
    pid = int(session.agent_pid or 0)
    if pid <= 0:
        return
    if pid == os.getpid():
        return
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return


def _find_browser_binary() -> str:
    candidates = (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    )
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("No supported Chromium browser found for persistent web session.")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _agent_ping(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def _wait_for_agent(port: int, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _agent_ping(port):
            return
        time.sleep(0.2)
    raise SystemExit(f"Timed out waiting for session control agent on port {port}")


def _cdp_alive(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def _cdp_primary_target(port: int) -> dict[str, Any] | None:
    url = f"http://127.0.0.1:{port}/json/list"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    pages = [item for item in payload if isinstance(item, dict) and item.get("type") == "page"]
    if not pages:
        return None
    return pages[0]


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_cdp(port: int, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.2)
    raise SystemExit(f"Timed out waiting for persistent browser session on port {port}")
