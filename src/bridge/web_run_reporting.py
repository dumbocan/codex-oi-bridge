"""Reporting helpers for web-run finalization and persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from bridge.models import OIReport


def persist_report_and_status(
    *,
    report: OIReport | None,
    run_dir: Path,
    task: str,
    write_json_fn: Callable[[Path, dict[str, Any]], None],
    write_status_fn: Callable[..., None],
) -> None:
    if report is None:
        return
    try:
        write_json_fn(run_dir / "report.json", report.to_dict())
    except Exception:
        pass
    try:
        write_status_fn(
            run_id=run_dir.name,
            run_dir=run_dir,
            task=task,
            result=report.result,
            state="completed",
            report_path=run_dir / "report.json",
            progress="web run finalized",
        )
    except Exception:
        pass


def build_web_report(
    *,
    run_id: str,
    url: str,
    actions: list[str],
    observations: list[str],
    console_errors: list[str],
    network_findings: list[str],
    ui_findings: list[str],
    result: str,
    evidence_paths: list[str],
) -> OIReport:
    return OIReport(
        task_id=run_id,
        goal=f"web: {url}",
        actions=actions,
        observations=observations,
        console_errors=console_errors,
        network_findings=network_findings,
        ui_findings=ui_findings,
        result=result,
        evidence_paths=evidence_paths,
    )
