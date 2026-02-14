"""Deterministic window management backend for GUI mode."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bridge.models import OIReport


_WINDOW_OP_RE = re.compile(r"window:(list|active|activate|open)", flags=re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def should_handle_window_task(task: str) -> bool:
    low = task.lower()
    if "window:" in low:
        return True
    keywords = (
        "lista ventanas",
        "listar ventanas",
        "ventana activa",
        "activar ventana",
        "abre ventana",
        "open window",
        "list windows",
        "active window",
        "activate window",
    )
    return any(item in low for item in keywords)


def run_window_task(task: str, run_dir: Path, timeout_seconds: int) -> OIReport:
    ops = _extract_ops(task)
    if not ops:
        raise SystemExit("GUI window mode requires explicit window operations.")

    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    actions: list[str] = []
    observations: list[str] = []
    console_errors: list[str] = []
    ui_findings: list[str] = []
    evidence_paths: list[str] = []

    for idx, op in enumerate(ops, start=1):
        before = evidence_dir / f"step_{idx}_before.png"
        after = evidence_dir / f"step_{idx}_after.png"
        window_txt = evidence_dir / f"step_{idx}_window.txt"
        _capture_screenshot(before, timeout_seconds, console_errors)

        if op["kind"] == "list":
            cmd = ["wmctrl", "-l"]
            actions.append("cmd: wmctrl -l")
            proc = _run_cmd(cmd, timeout_seconds)
            if proc.returncode == 0:
                lines = [line for line in proc.stdout.splitlines() if line.strip()]
                observations.append(f"step {idx} listed windows: {len(lines)} entries")
                ui_findings.append(f"step {idx} verify windows listed")
            else:
                console_errors.append(proc.stderr.strip() or "wmctrl -l failed")
        elif op["kind"] == "active":
            cmd = ["xdotool", "getactivewindow", "getwindowname"]
            actions.append("cmd: xdotool getactivewindow getwindowname")
            proc = _run_cmd(cmd, timeout_seconds)
            if proc.returncode == 0 and proc.stdout.strip():
                title = proc.stdout.strip()
                observations.append(f"step {idx} active window: {title}")
                ui_findings.append(f"step {idx} verify active window captured")
            else:
                console_errors.append(proc.stderr.strip() or "active window query failed")
        elif op["kind"] == "activate":
            target = op.get("target", "")
            activated = _activate_window(target, timeout_seconds, actions, observations, console_errors)
            if activated:
                ui_findings.append(f"step {idx} verify window activated")
            else:
                ui_findings.append(f"step {idx} verify activation failed")
        elif op["kind"] == "open":
            target = op.get("target", "")
            opened = _open_target(target, timeout_seconds, actions, observations, console_errors)
            if opened:
                ui_findings.append(f"step {idx} verify window open requested")
            else:
                ui_findings.append(f"step {idx} verify open failed")

        _capture_screenshot(after, timeout_seconds, console_errors)
        _write_window_evidence(window_txt, run_id=run_dir.name, step=idx, observations=observations)
        evidence_paths.append(_to_repo_rel(before))
        evidence_paths.append(_to_repo_rel(after))
        evidence_paths.append(_to_repo_rel(window_txt))

    result = "success"
    if console_errors:
        result = "partial" if observations else "failed"

    return OIReport(
        task_id=run_dir.name,
        goal=task,
        actions=actions,
        observations=observations,
        console_errors=console_errors,
        network_findings=[],
        ui_findings=ui_findings,
        result=result,
        evidence_paths=evidence_paths,
    )


def _extract_ops(task: str) -> list[dict[str, str]]:
    ops: list[dict[str, str]] = []
    matches = list(_WINDOW_OP_RE.finditer(task))
    for idx, match in enumerate(matches):
        kind = match.group(1).lower().strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(task)
        target = task[start:end].strip().strip("\"'“”")
        ops.append({"kind": kind, "target": target})
    if ops:
        return ops

    low = task.lower()
    if "lista ventanas" in low or "listar ventanas" in low or "list windows" in low:
        ops.append({"kind": "list", "target": ""})
    if "ventana activa" in low or "active window" in low:
        ops.append({"kind": "active", "target": ""})

    activate_match = re.search(
        r"(?:activar ventana|activate window)\s+[\"'“”]?([^\"'“”\n\r,;]+)",
        task,
        flags=re.IGNORECASE,
    )
    if activate_match:
        ops.append({"kind": "activate", "target": activate_match.group(1).strip()})

    open_match = re.search(
        r"(?:abrir ventana|open window|abre navegador|open browser)\s+[\"'“”]?([^\"'“”\n\r,;]+)",
        task,
        flags=re.IGNORECASE,
    )
    if open_match:
        ops.append({"kind": "open", "target": open_match.group(1).strip()})

    url_match = _URL_RE.search(task)
    if url_match and ("abr" in low or "open" in low or "navega" in low):
        ops.append({"kind": "open", "target": url_match.group(0)})

    return ops


def _activate_window(
    target: str,
    timeout_seconds: int,
    actions: list[str],
    observations: list[str],
    console_errors: list[str],
) -> bool:
    target = target.strip()
    if not target:
        console_errors.append("window:activate missing target")
        return False

    if target.startswith("0x"):
        cmd = ["wmctrl", "-ia", target]
        actions.append(f"cmd: wmctrl -ia {target}")
        proc = _run_cmd(cmd, timeout_seconds)
        if proc.returncode == 0:
            observations.append(f"activated window id {target}")
            return True
        console_errors.append(proc.stderr.strip() or "wmctrl -ia failed")
        return False

    list_proc = _run_cmd(["wmctrl", "-l"], timeout_seconds)
    actions.append("cmd: wmctrl -l")
    if list_proc.returncode != 0:
        console_errors.append(list_proc.stderr.strip() or "wmctrl -l failed")
        return False

    window_id = ""
    for line in list_proc.stdout.splitlines():
        if target.lower() in line.lower():
            window_id = line.split(maxsplit=1)[0]
            break

    if not window_id:
        console_errors.append(f"window target not found: {target}")
        return False

    cmd = ["wmctrl", "-ia", window_id]
    actions.append(f"cmd: wmctrl -ia {window_id}")
    proc = _run_cmd(cmd, timeout_seconds)
    if proc.returncode == 0:
        observations.append(f"activated window '{target}' ({window_id})")
        return True
    console_errors.append(proc.stderr.strip() or "wmctrl -ia failed")
    return False


def _open_target(
    target: str,
    timeout_seconds: int,
    actions: list[str],
    observations: list[str],
    console_errors: list[str],
) -> bool:
    target = target.strip()
    if not target:
        console_errors.append("window:open missing target")
        return False

    opener: list[str] | None = None
    if _is_url(target):
        if shutil.which("xdg-open"):
            opener = ["xdg-open", target]
        elif shutil.which("google-chrome"):
            opener = ["google-chrome", "--new-window", target]
        elif shutil.which("firefox"):
            opener = ["firefox", target]
    else:
        parts = shlex.split(target)
        if parts and shutil.which(parts[0]):
            opener = parts

    if opener is None:
        console_errors.append(f"window open target unsupported or unavailable: {target}")
        return False

    actions.append(f"cmd: {' '.join(opener)}")
    proc = _run_cmd(opener, timeout_seconds)
    if proc.returncode == 0:
        observations.append(f"open requested: {target}")
        return True
    console_errors.append(proc.stderr.strip() or "window open command failed")
    return False


def _capture_screenshot(path: Path, timeout_seconds: int, console_errors: list[str]) -> None:
    cmd: list[str] | None = None
    if shutil.which("scrot"):
        cmd = ["scrot", str(path)]
    elif shutil.which("import"):
        cmd = ["import", "-window", "root", str(path)]
    if cmd is None:
        console_errors.append("screenshot tool missing: scrot/import")
        return

    proc = _run_cmd(cmd, timeout_seconds)
    if proc.returncode != 0:
        console_errors.append(proc.stderr.strip() or f"{' '.join(cmd)} failed")


def _write_window_evidence(path: Path, *, run_id: str, step: int, observations: list[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        f"run_id: {run_id}",
        f"step: {step}",
        f"timestamp_utc: {now}",
        "window evidence generated by bridge window backend",
    ]
    for line in observations[-3:]:
        lines.append(f"- {line}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_cmd(cmd: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=min(timeout_seconds, 30),
        check=False,
    )


def _to_repo_rel(path: Path) -> str:
    return str(path.resolve().relative_to(Path.cwd()))


def _is_url(text: str) -> bool:
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
