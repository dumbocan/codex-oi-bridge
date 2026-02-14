"""CLI entrypoint for codex-oi-bridge."""

from __future__ import annotations

import argparse
import json
import os
import shlex
from pathlib import Path

from bridge.constants import ALLOWED_COMMAND_PREFIXES
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


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "run":
        run_command(args.task, confirm_sensitive=args.confirm_sensitive)
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
        "--confirm-sensitive",
        action="store_true",
        help="Approve sensitive observation actions without interactive prompt.",
    )

    subparsers.add_parser("status", help="Show latest run status")

    logs_parser = subparsers.add_parser("logs", help="Tail logs for latest run")
    logs_parser.add_argument("--tail", type=int, default=200)
    return parser


def run_command(task: str, confirm_sensitive: bool) -> None:
    if task_violates_code_edit_rule(task):
        raise SystemExit("Task rejected: requests source-code modification (forbidden by guardrails).")
    _validate_oi_runtime_config()
    sensitive_intent = task_has_sensitive_intent(task)
    require_sensitive_confirmation(sensitive_intent, auto_confirm=confirm_sensitive)

    ctx = create_run_context()
    append_log(ctx.bridge_log, f"run_id={ctx.run_id}")
    append_log(ctx.bridge_log, f"goal={task}")

    prompt = build_oi_prompt(
        task_id=ctx.run_id,
        task=task,
        run_dir=ctx.run_dir,
        allowlist=ALLOWED_COMMAND_PREFIXES,
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
    _validate_report_actions(report, confirm_sensitive)

    write_json(ctx.report_path, report.to_dict())
    write_status(
        run_id=ctx.run_id,
        run_dir=ctx.run_dir,
        task=task,
        result=report.result,
        report_path=ctx.report_path,
    )
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))


def _validate_report_actions(report: OIReport, confirm_sensitive: bool) -> None:
    sensitive_hits: list[str] = []
    for action in report.actions:
        if not action.startswith("cmd:"):
            continue
        command = action.split("cmd:", 1)[1].strip()
        decision = evaluate_command(command)
        if not decision.allowed:
            raise SystemExit(f"Guardrail blocked action '{command}': {decision.reason}")
        if decision.sensitive:
            sensitive_hits.append(command)
    require_sensitive_confirmation(sensitive_hits, auto_confirm=confirm_sensitive)


def logs_command(tail_count: int) -> None:
    payload = status_payload()
    if payload.get("status") == "no-runs":
        raise SystemExit("No runs available yet.")
    run_dir = Path(payload["run_dir"])
    bridge_log = run_dir / "bridge.log"
    oi_stderr = run_dir / "oi_stderr.log"
    output_lines = []
    output_lines.extend(tail_lines(bridge_log, tail_count))
    output_lines.extend(tail_lines(oi_stderr, tail_count))
    print("\n".join(output_lines))


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


if __name__ == "__main__":
    main()
