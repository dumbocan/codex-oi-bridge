import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bridge.cli import _validate_evidence_paths, _validate_report_actions, logs_command
from bridge.models import OIReport


class CLITests(unittest.TestCase):
    def test_actions_without_cmd_prefix_are_blocked(self) -> None:
        report = OIReport(
            task_id="t1",
            goal="inspect",
            actions=["attempt to check ui"],
            observations=[],
            console_errors=[],
            network_findings=[],
            ui_findings=[],
            result="failed",
            evidence_paths=[],
        )
        with self.assertRaises(SystemExit):
            _validate_report_actions(report, confirm_sensitive=True)

    def test_evidence_path_outside_run_dir_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            run_dir = base / "runs" / "r1"
            run_dir.mkdir(parents=True)
            outside = base / "outside.txt"
            outside.write_text("x", encoding="utf-8")
            report = OIReport(
                task_id="t1",
                goal="inspect",
                actions=["cmd: ls -la"],
                observations=[],
                console_errors=[],
                network_findings=[],
                ui_findings=[],
                result="failed",
                evidence_paths=[str(outside)],
            )
            with self.assertRaises(SystemExit):
                _validate_evidence_paths(report, run_dir)

    def test_logs_include_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            (run_dir / "bridge.log").write_text("bridge-line\n", encoding="utf-8")
            (run_dir / "oi_stdout.log").write_text("stdout-line\n", encoding="utf-8")
            (run_dir / "oi_stderr.log").write_text("stderr-line\n", encoding="utf-8")
            payload = {"run_dir": str(run_dir)}
            with patch("bridge.cli.status_payload", return_value=payload):
                out = io.StringIO()
                with redirect_stdout(out):
                    logs_command(200)
            text = out.getvalue()
            self.assertIn("bridge-line", text)
            self.assertIn("stdout-line", text)
            self.assertIn("stderr-line", text)


if __name__ == "__main__":
    unittest.main()
