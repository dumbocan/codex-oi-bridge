"""CLI entrypoint for codex-oi-bridge."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bridge.constants import (
    GUI_ALLOWED_COMMAND_PREFIXES,
    GUI_STATE_CHANGING_TOKENS,
    SHELL_ALLOWED_COMMAND_PREFIXES,
    WEB_ALLOWED_COMMAND_PREFIXES,
)
from bridge.guardrails import (
    evaluate_command,
    require_sensitive_confirmation,
    task_has_sensitive_intent,
    task_violates_code_edit_rule,
)
from bridge.models import OIReport
from bridge.parser import parse_oi_report
from bridge.runner import build_oi_prompt, run_open_interpreter
from bridge.storage import (
    append_log,
    create_run_context,
    status_payload,
    tail_lines,
    write_json,
    write_status,
)
from bridge.web_backend import run_web_task
from bridge.window_backend import run_window_task, should_handle_window_task


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_BUTTON_DIRECT_RE = re.compile(
    r"(?:button|bot[oó]n)\s*[=:]?\s*[\"'“”]([^\"'“”]{1,120})[\"'“”]",
    flags=re.IGNORECASE,
)
_CLICK_QUOTED_RE = re.compile(
    r"(?:click(?:\s+en)?|haz\s+click(?:\s+en)?)\s+[\"'“”]([^\"'“”]{1,120})[\"'“”]",
    flags=re.IGNORECASE,
)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "run":
        run_command(
            args.task,
            confirm_sensitive=args.confirm_sensitive,
            mode=args.mode,
            verified=args.verified,
        )
        return
    if args.command == "gui-run":
        run_command(
            args.task,
            confirm_sensitive=args.confirm_sensitive,
            mode="gui",
            verified=args.verified,
        )
        return
    if args.command == "web-run":
        run_command(
            args.task,
            confirm_sensitive=args.confirm_sensitive,
            mode="web",
            verified=args.verified,
        )
        return
    if args.command == "status":
        print(json.dumps(status_payload(), indent=2, ensure_ascii=False))
        return
    if args.command == "logs":
        logs_command(args.tail)
        return
    if args.command == "doctor":
        doctor_command(mode=args.mode)
        return

    parser.print_help()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge", description="Codex/Open-Interpreter bridge CLI.")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help='Run a task: bridge run "<task>"')
    run_parser.add_argument("task", type=str)
    run_parser.add_argument(
        "--mode",
        choices=("shell", "gui", "web"),
        default="shell",
        help="Execution mode. shell (default), gui, or web.",
    )
    run_parser.add_argument(
        "--confirm-sensitive",
        action="store_true",
        help="Approve sensitive observation actions without interactive prompt.",
    )
    run_parser.add_argument(
        "--verified",
        action="store_true",
        help="Enable strict verified mode checks before accepting run output.",
    )

    gui_run_parser = subparsers.add_parser(
        "gui-run",
        help='Run a GUI task: bridge gui-run "<task>"',
    )
    gui_run_parser.add_argument("task", type=str)
    gui_run_parser.add_argument(
        "--confirm-sensitive",
        action="store_true",
        help="Required for GUI tasks that can alter state.",
    )
    gui_run_parser.add_argument(
        "--verified",
        action="store_true",
        help="Enable strict verified mode checks before accepting run output.",
    )
    web_run_parser = subparsers.add_parser(
        "web-run",
        help='Run a deterministic web task: bridge web-run "<task>"',
    )
    web_run_parser.add_argument("task", type=str)
    web_run_parser.add_argument(
        "--confirm-sensitive",
        action="store_true",
        help="Approve sensitive actions without interactive prompt.",
    )
    web_run_parser.add_argument(
        "--verified",
        action="store_true",
        help="Enable strict verified mode checks before accepting run output.",
    )

    subparsers.add_parser("status", help="Show latest run status")

    logs_parser = subparsers.add_parser("logs", help="Tail logs for latest run")
    logs_parser.add_argument("--tail", type=int, default=200)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Validate runtime prerequisites",
    )
    doctor_parser.add_argument(
        "--mode",
        choices=("shell", "gui", "web"),
        default="shell",
        help="Check shell, gui, or web prerequisites.",
    )
    return parser


def run_command(task: str, confirm_sensitive: bool, mode: str, verified: bool = False) -> None:
    if task_violates_code_edit_rule(task):
        raise SystemExit("Task rejected: requests source-code modification (forbidden by guardrails).")
    use_window_backend = mode == "gui" and should_handle_window_task(task)
    _validate_mode_preconditions(mode, confirm_sensitive)
    preflight_mode = "gui-window" if use_window_backend else mode
    _preflight_runtime(preflight_mode)

    sensitive_intent = task_has_sensitive_intent(task)
    require_sensitive_confirmation(sensitive_intent, auto_confirm=confirm_sensitive)

    expected_targets = _extract_expected_targets(task)
    button_targets = _extract_button_targets(task)
    allowlist = _mode_allowlist(mode)

    ctx = create_run_context()
    if mode in ("gui", "web"):
        (ctx.run_dir / "evidence").mkdir(parents=True, exist_ok=True)
    append_log(ctx.bridge_log, f"run_id={ctx.run_id}")
    append_log(ctx.bridge_log, f"goal={task}")
    append_log(ctx.bridge_log, f"mode={mode}")
    if expected_targets:
        append_log(ctx.bridge_log, f"expected_targets={sorted(expected_targets)}")
    if button_targets:
        append_log(ctx.bridge_log, f"button_targets={sorted(button_targets)}")

    timeout_seconds = int(os.getenv("OI_BRIDGE_TIMEOUT_SECONDS", "300"))
    stdout_text = ""

    if mode == "web":
        report = run_web_task(task, run_dir=ctx.run_dir, timeout_seconds=timeout_seconds, verified=verified)
        stdout_text = json.dumps(report.to_dict(), ensure_ascii=False)
        ctx.stdout_log.write_text(stdout_text + "\n", encoding="utf-8")
        ctx.stderr_log.write_text("", encoding="utf-8")
        append_log(ctx.bridge_log, "runner=web-backend")
        append_log(ctx.bridge_log, "oi_returncode=0")
        append_log(ctx.bridge_log, "oi_timed_out=False")
        write_json(ctx.run_dir / "prompt.json", {"mode": "web", "task": task})
    elif use_window_backend:
        report = run_window_task(task, run_dir=ctx.run_dir, timeout_seconds=timeout_seconds)
        stdout_text = json.dumps(report.to_dict(), ensure_ascii=False)
        ctx.stdout_log.write_text(stdout_text + "\n", encoding="utf-8")
        ctx.stderr_log.write_text("", encoding="utf-8")
        append_log(ctx.bridge_log, "runner=window-backend")
        append_log(ctx.bridge_log, "oi_returncode=0")
        append_log(ctx.bridge_log, "oi_timed_out=False")
        write_json(ctx.run_dir / "prompt.json", {"mode": "gui-window", "task": task})
    else:
        _validate_oi_runtime_config()
        prompt = build_oi_prompt(
            task_id=ctx.run_id,
            task=task,
            run_dir=ctx.run_dir,
            allowlist=allowlist,
            mode=mode,
        )
        write_json(ctx.run_dir / "prompt.json", {"prompt": prompt})

        result = run_open_interpreter(
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            run_dir=ctx.run_dir,
        )
        stdout_text = result.stdout
        ctx.stdout_log.write_text(result.stdout, encoding="utf-8")
        ctx.stderr_log.write_text(result.stderr, encoding="utf-8")
        append_log(ctx.bridge_log, f"oi_returncode={result.returncode}")
        append_log(ctx.bridge_log, f"oi_timed_out={result.timed_out}")

        try:
            report = parse_oi_report(result.stdout)
        except ValueError as exc:
            write_status(
                run_id=ctx.run_id,
                run_dir=ctx.run_dir,
                task=task,
                result="failed",
                report_path=ctx.report_path,
            )
            message = str(exc)
            if "OpenAI API key not found" in result.stdout:
                message = (
                    "Open Interpreter requires API key/model configuration. "
                    "Set OPENAI_API_KEY and retry."
                )
            if result.timed_out:
                message = (
                    f"Open Interpreter timed out after {timeout_seconds}s "
                    "without producing a valid report JSON"
                )
            raise SystemExit(
                f"Open Interpreter output is not valid JSON: {message}. "
                f"Inspect {ctx.stdout_log} and {ctx.stderr_log}"
            )
        if result.returncode != 0:
            append_log(
                ctx.bridge_log,
                "warning=non-zero-returncode-but-valid-report-parsed",
            )

    click_steps = _validate_report_actions(
        report,
        confirm_sensitive,
        expected_targets=expected_targets,
        allowlist=allowlist,
        mode=mode,
    )
    safe_evidence_paths = _validate_evidence_paths(
        report,
        ctx.run_dir,
        mode=mode,
        click_steps=click_steps,
        run_id=ctx.run_id,
    )
    report = replace(report, evidence_paths=safe_evidence_paths)
    _validate_gui_post_conditions(
        report,
        mode=mode,
        click_steps=click_steps,
        button_targets=button_targets,
    )
    _validate_verified_mode(
        report,
        mode=mode,
        verified=verified,
        stdout_text=stdout_text,
    )

    write_json(ctx.report_path, report.to_dict())
    write_status(
        run_id=ctx.run_id,
        run_dir=ctx.run_dir,
        task=task,
        result=report.result,
        report_path=ctx.report_path,
    )
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))


def _validate_report_actions(
    report: OIReport,
    confirm_sensitive: bool,
    *,
    expected_targets: set[str] | None,
    allowlist: tuple[str, ...],
    mode: str,
) -> int:
    sensitive_hits: list[str] = []
    expected = expected_targets or set()
    click_steps = 0
    explicit_window_target_seen = False
    saw_mousemove_since_last_target = False

    for action in report.actions:
        if not action.startswith("cmd:"):
            raise SystemExit(
                "Guardrail blocked action: every action must follow format "
                "'cmd: <command>'."
            )
        command = action.split("cmd:", 1)[1].strip()
        if not command:
            raise SystemExit("Guardrail blocked action: empty command after 'cmd:'.")
        if "\n" in command or "\r" in command:
            raise SystemExit("Malformed command: multiline commands are not allowed.")

        decision = evaluate_command(command, allowlist=allowlist)
        if not decision.allowed:
            raise SystemExit(f"Guardrail blocked action '{command}': {decision.reason}")
        _validate_malformed_command(command)

        _validate_command_targets(command, expected)

        if mode == "gui":
            if _is_window_target_command(command):
                explicit_window_target_seen = True
                saw_mousemove_since_last_target = False
            if _is_mousemove_command(command):
                saw_mousemove_since_last_target = True
            if _is_coordinate_click(command):
                raise SystemExit("Guardrail blocked coordinate-based click without safe fallback.")
            if _is_click_command(command):
                click_steps += 1
                if saw_mousemove_since_last_target:
                    raise SystemExit(
                        "Guardrail blocked click: coordinate-based sequence "
                        "detected (mousemove + click)."
                    )
                if not explicit_window_target_seen:
                    raise SystemExit("Guardrail blocked click without explicit target window step.")
                sensitive_hits.append(command)
                saw_mousemove_since_last_target = False
            if _is_state_changing_gui_action(command):
                sensitive_hits.append(command)
        elif mode == "web":
            if _is_web_click_command(command):
                click_steps += 1

        if decision.sensitive:
            sensitive_hits.append(command)

    if mode == "gui" and click_steps > 0 and not confirm_sensitive:
        raise SystemExit("GUI state-changing actions require --confirm-sensitive.")
    require_sensitive_confirmation(sorted(set(sensitive_hits)), auto_confirm=confirm_sensitive)
    return click_steps


def logs_command(tail_count: int) -> None:
    payload = status_payload()
    if payload.get("status") == "no-runs":
        raise SystemExit("No runs available yet.")
    run_dir = Path(payload["run_dir"])
    bridge_log = run_dir / "bridge.log"
    oi_stdout = run_dir / "oi_stdout.log"
    oi_stderr = run_dir / "oi_stderr.log"
    output_lines = []
    output_lines.extend(tail_lines(bridge_log, tail_count))
    output_lines.extend(tail_lines(oi_stdout, tail_count))
    output_lines.extend(tail_lines(oi_stderr, tail_count))
    print("\n".join(output_lines))


def doctor_command(mode: str) -> None:
    checks = _collect_runtime_checks(mode)
    ok = all(item["ok"] for item in checks)
    print(json.dumps({"mode": mode, "ok": ok, "checks": checks}, indent=2, ensure_ascii=False))
    if not ok:
        raise SystemExit(1)


def _validate_evidence_paths(
    report: OIReport,
    run_dir: Path,
    *,
    mode: str,
    click_steps: int,
    run_id: str,
) -> list[str]:
    if mode == "gui" and click_steps > 0:
        report = _synthesize_gui_window_evidence(report, run_dir, click_steps, run_id)

    run_root = run_dir.resolve()
    safe_paths: list[str] = []
    rel_paths: list[str] = []

    for raw_path in report.evidence_paths:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            resolved = (Path.cwd() / candidate).resolve(strict=False)
        if run_root == resolved or run_root in resolved.parents:
            if not resolved.exists() or not resolved.is_file():
                raise SystemExit(
                    "Guardrail blocked evidence path: file missing or not a file: "
                    f"{raw_path}"
                )
            rel = resolved.relative_to(Path.cwd())
            safe_paths.append(str(rel))
            rel_paths.append(str(resolved.relative_to(run_root)))
            continue
        raise SystemExit(
            "Guardrail blocked evidence path outside run directory: "
            f"{raw_path}"
        )

    if mode == "gui" and click_steps > 0:
        existing = set(rel_paths)
        for step in range(1, click_steps + 1):
            required = (
                f"evidence/step_{step}_before.png",
                f"evidence/step_{step}_after.png",
                f"evidence/step_{step}_window.txt",
            )
            for rel in required:
                if rel not in existing:
                    raise SystemExit(
                        "Guardrail blocked GUI report: missing required evidence "
                        f"for click step {step}: {rel}"
                    )
                full = (run_dir / rel).resolve(strict=False)
                if not full.exists() or not full.is_file():
                    raise SystemExit(
                        "Guardrail blocked GUI report: required evidence file "
                        "missing on disk. "
                        f"step={step}, path={rel}, run_dir={run_dir}"
                    )
                if rel.endswith(("_before.png", "_after.png")) and full.stat().st_size <= 0:
                    raise SystemExit(f"Screenshot evidence missing/empty for step {step}: {full}")
    if mode == "web" and click_steps > 0:
        existing = set(rel_paths)
        for step in range(1, click_steps + 1):
            required = (
                f"evidence/step_{step}_before.png",
                f"evidence/step_{step}_after.png",
            )
            for rel in required:
                if rel not in existing:
                    raise SystemExit(
                        "Guardrail blocked WEB report: missing required evidence "
                        f"for click step {step}: {rel}"
                    )
                full = (run_dir / rel).resolve(strict=False)
                if not full.exists() or not full.is_file():
                    raise SystemExit(
                        "Guardrail blocked WEB report: required evidence file missing "
                        f"on disk. step={step}, path={rel}, run_dir={run_dir}"
                    )
                if full.stat().st_size <= 0:
                    raise SystemExit(f"Screenshot evidence missing/empty for step {step}: {full}")
    return safe_paths


def _synthesize_gui_window_evidence(
    report: OIReport,
    run_dir: Path,
    click_steps: int,
    run_id: str,
) -> OIReport:
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    merged_paths = list(report.evidence_paths)
    path_set = set(merged_paths)
    step_lines = report.observations + report.ui_findings
    now = datetime.now(timezone.utc).isoformat()

    for step in range(1, click_steps + 1):
        abs_path = evidence_dir / f"step_{step}_window.txt"
        rel = str(abs_path.resolve().relative_to(Path.cwd()))
        if not abs_path.exists():
            related = [
                line
                for line in step_lines
                if any(token in line.lower() for token in (f"step {step}", f"step_{step}", f"paso {step}"))
            ]
            content = [
                f"run_id: {run_id}",
                f"step: {step}",
                f"timestamp_utc: {now}",
                "window evidence synthesized by bridge from run logs",
            ]
            if related:
                content.append("related_findings:")
                content.extend(f"- {line}" for line in related[:5])
            abs_path.write_text("\n".join(content) + "\n", encoding="utf-8")
        if rel not in path_set:
            merged_paths.append(rel)
            path_set.add(rel)

    return replace(report, evidence_paths=merged_paths)


def _validate_gui_post_conditions(
    report: OIReport,
    *,
    mode: str,
    click_steps: int,
    button_targets: set[str],
) -> None:
    if mode not in ("gui", "web"):
        return

    lines = [line.lower() for line in (report.observations + report.ui_findings)]
    combined = " ".join(lines)
    verify_tokens = ("verify", "verified", "cambio", "changed", "visible", "result")

    for step in range(1, click_steps + 1):
        step_tokens = (f"step {step}", f"step_{step}", f"paso {step}")
        step_lines = [line for line in lines if any(token in line for token in step_tokens)]
        if not step_lines:
            raise SystemExit(
                "Guardrail blocked GUI report: missing step marker in "
                f"observations/ui_findings for click step {step}."
            )
        if not any(any(token in line for token in verify_tokens) for line in step_lines):
            raise SystemExit(
                "Guardrail blocked GUI report: missing verify post-click "
                f"details for click step {step}."
            )

    if mode == "gui":
        for label in button_targets:
            if label.lower() not in combined:
                raise SystemExit(
                    "Guardrail blocked GUI report: task mentions button text "
                    f"'{label}' but findings do not confirm location/action/result."
                )


def _validate_oi_runtime_config() -> None:
    args = shlex.split(os.getenv("OI_BRIDGE_ARGS", ""))
    local_mode = any(token in ("--local", "--offline") for token in args)
    if local_mode:
        raise SystemExit(
            "Local/offline mode is interactive in current Open Interpreter builds "
            "and is not supported by this non-interactive bridge. "
            "Use OPENAI_API_KEY with cloud mode."
        )
    if os.getenv("OPENAI_API_KEY"):
        return
    raise SystemExit(
        "Missing OPENAI_API_KEY. "
        "Export OPENAI_API_KEY and rerun."
    )


def _validate_mode_preconditions(mode: str, confirm_sensitive: bool) -> None:
    if mode == "gui" and not confirm_sensitive:
        raise SystemExit("GUI mode requires explicit --confirm-sensitive.")


def _preflight_runtime(mode: str) -> None:
    checks = _collect_runtime_checks(mode)
    failed = [item for item in checks if not item["ok"]]
    if failed:
        doctor_mode = "gui" if mode == "gui-window" else mode
        summary = "; ".join(item["name"] for item in failed)
        raise SystemExit(
            "Runtime preflight failed: "
            f"{summary}. Run `bridge doctor --mode {doctor_mode}` for details."
        )


def _collect_runtime_checks(mode: str) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    if mode in ("shell", "gui"):
        has_key = bool(os.getenv("OPENAI_API_KEY"))
        add(
            "openai_api_key",
            has_key,
            "OPENAI_API_KEY present" if has_key else "Missing OPENAI_API_KEY",
        )

        dns_ok = _can_resolve("api.openai.com")
        add(
            "dns_api_openai",
            dns_ok,
            "api.openai.com resolvable" if dns_ok else "Cannot resolve api.openai.com",
        )

    if mode in ("shell", "gui"):
        interpreter_path = shutil.which(os.getenv("OI_BRIDGE_COMMAND", "interpreter")) or str(
            Path(".venv") / "bin" / os.getenv("OI_BRIDGE_COMMAND", "interpreter")
        )
        add(
            "interpreter_binary",
            Path(interpreter_path).exists()
            or bool(shutil.which(os.getenv("OI_BRIDGE_COMMAND", "interpreter"))),
            f"Using {interpreter_path}",
        )

    if mode in ("gui", "gui-window"):
        display = os.getenv("DISPLAY", "")
        add("display_env", bool(display), f"DISPLAY={display or '<unset>'}")
        for cmd in ("xdotool", "wmctrl", "xwininfo"):
            found = shutil.which(cmd) is not None
            add(f"tool_{cmd}", found, f"{cmd} {'found' if found else 'missing'}")
        screenshot_found = (shutil.which("scrot") is not None) or (shutil.which("import") is not None)
        add(
            "tool_screenshot",
            screenshot_found,
            "scrot/import available" if screenshot_found else "Missing both scrot and import",
        )
        shot_ok, shot_detail = _doctor_screenshot_runtime_check()
        add("screenshot_runtime", shot_ok, shot_detail)
    if mode == "web":
        py_ok = _playwright_module_available()
        add(
            "playwright_python",
            py_ok,
            "playwright module importable" if py_ok else "playwright module missing",
        )
        browser_ok = _web_browser_binary_available()
        add(
            "web_browser_binary",
            browser_ok,
            "chrome/chromium/firefox binary found"
            if browser_ok
            else "No browser binary found in PATH",
        )

    return checks


def _can_resolve(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except OSError:
        return False


def _playwright_module_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _web_browser_binary_available() -> bool:
    candidates = (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "firefox",
    )
    return any(shutil.which(item) for item in candidates)


def _doctor_screenshot_runtime_check() -> tuple[bool, str]:
    doctor_dir = Path("runs") / ".doctor"
    doctor_dir.mkdir(parents=True, exist_ok=True)
    out_file = doctor_dir / "doctor_screenshot.png"
    if out_file.exists():
        out_file.unlink()

    cmd: list[str] | None = None
    if shutil.which("scrot"):
        cmd = ["scrot", str(out_file)]
    elif shutil.which("import"):
        cmd = ["import", "-window", "root", str(out_file)]
    if cmd is None:
        return False, "No screenshot binary available (scrot/import)."

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    ok = proc.returncode == 0 and out_file.exists() and out_file.is_file() and out_file.stat().st_size > 0
    detail = f"cmd={' '.join(cmd)} rc={proc.returncode}"
    if proc.stderr.strip():
        detail = f"{detail} stderr={proc.stderr.strip()[:120]}"
    return ok, detail


def _mode_allowlist(mode: str) -> tuple[str, ...]:
    if mode == "gui":
        return GUI_ALLOWED_COMMAND_PREFIXES
    if mode == "web":
        return WEB_ALLOWED_COMMAND_PREFIXES
    return SHELL_ALLOWED_COMMAND_PREFIXES


def _extract_expected_targets(task: str) -> set[str]:
    targets: set[str] = set()
    for raw in _URL_RE.findall(task):
        origin = _origin(raw)
        if origin:
            targets.add(origin)
    return targets


def _extract_urls(text: str) -> list[str]:
    return _URL_RE.findall(text)


def _extract_button_targets(task: str) -> set[str]:
    targets: set[str] = set()
    lowered = task.lower()
    has_button_word = ("button" in lowered) or ("boton" in lowered) or ("botón" in lowered)

    for match in _BUTTON_DIRECT_RE.finditer(task):
        label = match.group(1).strip()
        if label and _origin(label) is None:
            targets.add(label)

    for match in _CLICK_QUOTED_RE.finditer(task):
        label = match.group(1).strip()
        if not label or _origin(label) is not None:
            continue
        if not has_button_word:
            continue
        start, end = match.span()
        window = task[max(0, start - 40):min(len(task), end + 40)].lower()
        if "button" in window or "boton" in window or "botón" in window:
            targets.add(label)
    return targets


def _origin(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_command_targets(command: str, expected_targets: set[str]) -> None:
    if not expected_targets:
        return
    parts = shlex.split(command)
    if not parts:
        return
    if parts[0] not in ("curl", "wget"):
        return

    urls = _extract_urls(command)
    if not urls:
        raise SystemExit(
            "Guardrail blocked network action without explicit URL while task "
            "requires specific target(s)."
        )

    for raw_url in urls:
        origin = _origin(raw_url)
        if not origin:
            raise SystemExit(f"Guardrail blocked malformed URL in action: {raw_url}")
        if origin not in expected_targets:
            expected = ", ".join(sorted(expected_targets))
            raise SystemExit(
                "Guardrail blocked URL target drift. "
                f"Observed: {origin}. Expected one of: {expected}"
            )


def _is_window_target_command(command: str) -> bool:
    low = command.lower()
    return any(
        token in low
        for token in (
            "xdotool search --name",
            "xdotool search --class",
            "xdotool search --classname",
            "xdotool windowactivate",
            "xdotool windowfocus",
            "xwininfo -name",
            "xwininfo -id",
        )
    )


def _is_click_command(command: str) -> bool:
    return "xdotool click" in command.lower()


def _is_coordinate_click(command: str) -> bool:
    low = command.lower()
    return "mousemove" in low and "click" in low


def _is_mousemove_command(command: str) -> bool:
    return "xdotool mousemove" in command.lower()


def _is_state_changing_gui_action(command: str) -> bool:
    low = command.lower()
    return any(token in low for token in GUI_STATE_CHANGING_TOKENS)


def _is_web_click_command(command: str) -> bool:
    low = command.lower()
    return low.startswith("playwright ") and " click " in f" {low} "


def _validate_malformed_command(command: str) -> None:
    try:
        parts = shlex.split(command)
    except ValueError:
        raise SystemExit("Malformed command: shell parsing failed.") from None
    if not parts:
        raise SystemExit("Malformed command: empty command payload.")
    if parts[0].startswith("-"):
        raise SystemExit("Malformed command: missing executable prefix.")


def _validate_verified_mode(
    report: OIReport,
    *,
    mode: str,
    verified: bool,
    stdout_text: str,
) -> None:
    if not verified:
        return
    if mode == "gui":
        return
    has_observable = any(
        (
            report.observations,
            report.console_errors,
            report.network_findings,
            report.ui_findings,
        )
    )
    if report.actions and (not stdout_text.strip() or not has_observable):
        raise SystemExit(
            "Verified mode failed: shell/api run lacks observable non-empty output."
        )


if __name__ == "__main__":
    main()
