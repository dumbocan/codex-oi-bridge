"""CLI entrypoint for codex-oi-bridge."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from bridge.constants import (
    GUI_ALLOWED_COMMAND_PREFIXES,
    GUI_STATE_CHANGING_TOKENS,
    SHELL_ALLOWED_COMMAND_PREFIXES,
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
        )
        return
    if args.command == "gui-run":
        run_command(args.task, confirm_sensitive=args.confirm_sensitive, mode="gui")
        return
    if args.command == "status":
        print(json.dumps(status_payload(), indent=2, ensure_ascii=False))
        return
    if args.command == "logs":
        logs_command(args.tail)
        return

    parser.print_help()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge", description="Codex/Open-Interpreter bridge CLI.")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help='Run a task: bridge run "<task>"')
    run_parser.add_argument("task", type=str)
    run_parser.add_argument(
        "--mode",
        choices=("shell", "gui"),
        default="shell",
        help="Execution mode. shell (default) or gui.",
    )
    run_parser.add_argument(
        "--confirm-sensitive",
        action="store_true",
        help="Approve sensitive observation actions without interactive prompt.",
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

    subparsers.add_parser("status", help="Show latest run status")

    logs_parser = subparsers.add_parser("logs", help="Tail logs for latest run")
    logs_parser.add_argument("--tail", type=int, default=200)
    return parser


def run_command(task: str, confirm_sensitive: bool, mode: str) -> None:
    if task_violates_code_edit_rule(task):
        raise SystemExit("Task rejected: requests source-code modification (forbidden by guardrails).")
    _validate_oi_runtime_config()
    _validate_mode_preconditions(mode, confirm_sensitive)

    sensitive_intent = task_has_sensitive_intent(task)
    require_sensitive_confirmation(sensitive_intent, auto_confirm=confirm_sensitive)

    expected_targets = _extract_expected_targets(task)
    button_targets = _extract_button_targets(task)
    allowlist = _mode_allowlist(mode)

    ctx = create_run_context()
    append_log(ctx.bridge_log, f"run_id={ctx.run_id}")
    append_log(ctx.bridge_log, f"goal={task}")
    append_log(ctx.bridge_log, f"mode={mode}")
    if expected_targets:
        append_log(ctx.bridge_log, f"expected_targets={sorted(expected_targets)}")
    if button_targets:
        append_log(ctx.bridge_log, f"button_targets={sorted(button_targets)}")

    prompt = build_oi_prompt(
        task_id=ctx.run_id,
        task=task,
        run_dir=ctx.run_dir,
        allowlist=allowlist,
        mode=mode,
    )
    write_json(ctx.run_dir / "prompt.json", {"prompt": prompt})

    timeout_seconds = int(os.getenv("OI_BRIDGE_TIMEOUT_SECONDS", "300"))
    result = run_open_interpreter(prompt=prompt, timeout_seconds=timeout_seconds)
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
    )
    report = replace(report, evidence_paths=safe_evidence_paths)
    _validate_gui_post_conditions(
        report,
        mode=mode,
        click_steps=click_steps,
        button_targets=button_targets,
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

        decision = evaluate_command(command, allowlist=allowlist)
        if not decision.allowed:
            raise SystemExit(f"Guardrail blocked action '{command}': {decision.reason}")

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


def _validate_evidence_paths(
    report: OIReport,
    run_dir: Path,
    *,
    mode: str,
    click_steps: int,
) -> list[str]:
    run_root = run_dir.resolve()
    safe_paths: list[str] = []
    rel_paths: list[Path] = []

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
            rel_paths.append(Path(rel).relative_to(run_dir))
            continue
        raise SystemExit(
            "Guardrail blocked evidence path outside run directory: "
            f"{raw_path}"
        )

    if mode == "gui" and click_steps > 0:
        existing = {str(path) for path in rel_paths}
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
                        f"missing on disk for click step {step}: {rel}"
                    )
    return safe_paths


def _validate_gui_post_conditions(
    report: OIReport,
    *,
    mode: str,
    click_steps: int,
    button_targets: set[str],
) -> None:
    if mode != "gui":
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


def _mode_allowlist(mode: str) -> tuple[str, ...]:
    if mode == "gui":
        return GUI_ALLOWED_COMMAND_PREFIXES
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


if __name__ == "__main__":
    main()
