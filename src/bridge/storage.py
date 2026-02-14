"""File storage helpers for run artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUNS_DIR = Path("runs")
STATUS_PATH = RUNS_DIR / "status.json"


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_dir: Path
    bridge_log: Path
    stdout_log: Path
    stderr_log: Path
    report_path: Path


def create_run_context() -> RunContext:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_dir: Path | None = None
    run_id = ""
    for attempt in range(100):
        base = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        suffix = f"-{attempt:02d}" if attempt else ""
        run_id = f"{base}{suffix}"
        candidate = RUNS_DIR / run_id
        if candidate.exists():
            continue
        candidate.mkdir(parents=True, exist_ok=False)
        run_dir = candidate
        break
    if run_dir is None:
        raise RuntimeError("Could not allocate unique run directory")
    return RunContext(
        run_id=run_id,
        run_dir=run_dir,
        bridge_log=run_dir / "bridge.log",
        stdout_log=run_dir / "oi_stdout.log",
        stderr_log=run_dir / "oi_stderr.log",
        report_path=run_dir / "report.json",
    )


def append_log(path: Path, message: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(message.rstrip() + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def write_status(
    *,
    run_id: str,
    run_dir: Path,
    task: str,
    result: str,
    report_path: Path,
) -> None:
    write_json(
        STATUS_PATH,
        {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "task": task,
            "result": result,
            "report_path": str(report_path),
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    )


def status_payload() -> dict[str, Any]:
    if not STATUS_PATH.exists():
        return {"status": "no-runs"}
    with STATUS_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def tail_lines(path: Path, line_count: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        lines = fh.readlines()
    return [line.rstrip("\n") for line in lines[-line_count:]]
