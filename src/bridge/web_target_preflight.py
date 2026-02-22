"""Target/network preflight checks for web-run."""

from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse


def preflight_target_reachable(
    url: str,
    timeout_seconds: float = 1.2,
    *,
    create_connection_fn=socket.create_connection,
) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if not host or port <= 0:
        raise SystemExit(f"Web target not reachable: {url}")

    # Many dev servers bind IPv4 only; prefer 127.0.0.1 first for localhost.
    candidates: list[str] = [host]
    if host in {"localhost", "0.0.0.0"}:
        candidates = ["127.0.0.1", "localhost", "::1"]

    last_exc: Exception | None = None
    for cand in candidates:
        try:
            with create_connection_fn((cand, int(port)), timeout=timeout_seconds):
                return
        except Exception as exc:  # pragma: no cover (covered via raised SystemExit)
            last_exc = exc
            continue
    raise SystemExit(f"Web target not reachable: {url}") from last_exc


def http_quick_check(url: str, timeout_seconds: float = 1.2) -> None:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        if int(resp.status) < 200 or int(resp.status) >= 400:
            raise SystemExit(f"Start your stack first: {url} returned {resp.status}")


def preflight_stack_prereqs(http_quick_check_fn=http_quick_check) -> None:
    # Optional project-specific stack preflight. Enabled via env to avoid hardcoding.
    # Example:
    #   export BRIDGE_WEB_PREFLIGHT_STACK=1
    #   export BRIDGE_WEB_PREFLIGHT_BACKEND_HEALTH_URL=http://127.0.0.1:8010/health
    #   export BRIDGE_WEB_PREFLIGHT_FRONTEND_URL=http://127.0.0.1:5181/
    stack_enabled = os.getenv("BRIDGE_WEB_PREFLIGHT_STACK", "0").strip() == "1"
    backend = os.getenv("BRIDGE_WEB_PREFLIGHT_BACKEND_HEALTH_URL", "").strip()
    frontend = os.getenv("BRIDGE_WEB_PREFLIGHT_FRONTEND_URL", "").strip()
    if stack_enabled and not backend:
        backend = "http://127.0.0.1:8010/health"
    if stack_enabled and not frontend:
        frontend = "http://127.0.0.1:5181/"
    if not backend and not frontend:
        return
    try:
        if backend:
            http_quick_check_fn(backend)
        if frontend:
            http_quick_check_fn(frontend)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit("Start your stack first") from exc
