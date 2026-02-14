"""Open Interpreter execution wrapper."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunnerResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


def build_oi_prompt(
    task_id: str,
    task: str,
    run_dir: Path,
    allowlist: tuple[str, ...],
    *,
    mode: str,
) -> str:
    allowed = ", ".join(allowlist)
    mode_block = _gui_mode_block(run_dir) if mode == "gui" else _shell_mode_block()
    return f"""
You are Open Interpreter used only as a screen/operation observer.
Never edit source code or architecture. Never execute destructive commands.
Allowed shell command prefixes only: {allowed}
Use shell commands only; do not use Python computer/display APIs, notebooks, or interactive setup flows.
Every shell action must be represented in actions[] as: "cmd: <exact command>".
If the goal includes explicit URLs, hosts, or ports, use them exactly and do not rewrite them.
Execution mode: {mode}
{mode_block}
If a requested step needs an action outside guardrails, do not execute it and report it.
Save evidence (logs/screenshots/reports) only inside: {run_dir}
Always return a single strict JSON object with keys exactly:
task_id, goal, actions, observations, console_errors, network_findings,
ui_findings, result, evidence_paths
No markdown, no explanations outside JSON.

task_id: {task_id}
goal: {task}
""".strip()


def run_open_interpreter(prompt: str, timeout_seconds: int) -> RunnerResult:
    command = os.getenv("OI_BRIDGE_COMMAND", "interpreter").strip()
    command = _resolve_command(command)
    args = _normalize_args(shlex.split(os.getenv("OI_BRIDGE_ARGS", "")))
    args = _ensure_non_interactive_args(args)
    prompt = _prompt_for_stdin_mode(prompt)
    try:
        proc = subprocess.run(
            [command, *args],
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return RunnerResult(
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(
            "utf-8", errors="replace"
        )
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(
            "utf-8", errors="replace"
        )
        return RunnerResult(
            stdout=stdout,
            stderr=stderr,
            returncode=124,
            timed_out=True,
        )


def _resolve_command(command: str) -> str:
    if os.path.sep in command:
        return command
    found = shutil.which(command)
    if found:
        return found
    venv_candidate = Path(".venv") / "bin" / command
    if venv_candidate.exists():
        return str(venv_candidate)
    return command


def _normalize_args(args: list[str]) -> list[str]:
    normalized: list[str] = []
    for token in args:
        if token == "--yes":
            normalized.append("-y")
        else:
            normalized.append(token)
    return normalized


def _ensure_non_interactive_args(args: list[str]) -> list[str]:
    ensured = list(args)
    if "--stdin" not in ensured and "-s" not in ensured:
        ensured.append("--stdin")
    if "--plain" not in ensured and "-pl" not in ensured:
        ensured.append("--plain")
    return ensured


def _prompt_for_stdin_mode(prompt: str) -> str:
    # --stdin uses input(), so only the first line is consumed.
    collapsed = " ".join(line.strip() for line in prompt.splitlines() if line.strip())
    return collapsed + "\n"


def _shell_mode_block() -> str:
    return (
        "In shell mode, focus on command output and direct observations. "
        "Do not simulate GUI interactions."
    )


def _gui_mode_block(run_dir: Path) -> str:
    evidence_dir = run_dir / "evidence"
    return (
        "In gui mode: no asumir, verificar. Un paso, una evidencia. "
        f"The evidence directory already exists: {evidence_dir}. "
        "Before any click, identify explicit target window/title. "
        "After each click, run a verify step describing what changed. "
        "For every click step N, save before/after screenshots in "
        f"{evidence_dir} as step_N_before.png and step_N_after.png. "
        "The bridge auto-finalizes step_N_window.txt if missing. "
        "If button/target is not found, report blocked state and safe alternatives."
    )
