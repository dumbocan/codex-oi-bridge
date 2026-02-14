import tempfile
import unittest
from pathlib import Path

from bridge.cli import (
    _extract_button_targets,
    _validate_evidence_paths,
    _validate_gui_post_conditions,
    _validate_report_actions,
)
from bridge.constants import GUI_ALLOWED_COMMAND_PREFIXES
from bridge.models import OIReport


class GUIModeTests(unittest.TestCase):
    def test_gui_rejects_click_without_target_window(self) -> None:
        report = OIReport(
            task_id="g1",
            goal="gui",
            actions=["cmd: xdotool click 1"],
            observations=[],
            console_errors=[],
            network_findings=[],
            ui_findings=[],
            result="failed",
            evidence_paths=[],
        )
        with self.assertRaises(SystemExit):
            _validate_report_actions(
                report,
                confirm_sensitive=True,
                expected_targets=set(),
                allowlist=GUI_ALLOWED_COMMAND_PREFIXES,
                mode="gui",
            )

    def test_gui_rejects_coordinate_click(self) -> None:
        report = OIReport(
            task_id="g2",
            goal="gui",
            actions=[
                "cmd: xdotool search --name Browser",
                "cmd: xdotool mousemove 10 20 click 1",
            ],
            observations=[],
            console_errors=[],
            network_findings=[],
            ui_findings=[],
            result="failed",
            evidence_paths=[],
        )
        with self.assertRaises(SystemExit):
            _validate_report_actions(
                report,
                confirm_sensitive=True,
                expected_targets=set(),
                allowlist=GUI_ALLOWED_COMMAND_PREFIXES,
                mode="gui",
            )

    def test_gui_rejects_mousemove_then_click_in_separate_commands(self) -> None:
        report = OIReport(
            task_id="g2b",
            goal="gui",
            actions=[
                "cmd: xdotool search --name Browser",
                "cmd: xdotool mousemove 20 30",
                "cmd: xdotool click 1",
            ],
            observations=[],
            console_errors=[],
            network_findings=[],
            ui_findings=[],
            result="failed",
            evidence_paths=[],
        )
        with self.assertRaises(SystemExit):
            _validate_report_actions(
                report,
                confirm_sensitive=True,
                expected_targets=set(),
                allowlist=GUI_ALLOWED_COMMAND_PREFIXES,
                mode="gui",
            )

    def test_gui_requires_confirm_sensitive_for_clicks(self) -> None:
        report = OIReport(
            task_id="g3",
            goal="gui",
            actions=[
                "cmd: xdotool search --name Browser",
                "cmd: xdotool click 1",
            ],
            observations=[],
            console_errors=[],
            network_findings=[],
            ui_findings=[],
            result="failed",
            evidence_paths=[],
        )
        with self.assertRaises(SystemExit):
            _validate_report_actions(
                report,
                confirm_sensitive=False,
                expected_targets=set(),
                allowlist=GUI_ALLOWED_COMMAND_PREFIXES,
                mode="gui",
            )

    def test_gui_evidence_requires_before_after_and_window_log(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            report = OIReport(
                task_id="g4",
                goal="gui",
                actions=["cmd: xdotool search --name Browser", "cmd: xdotool click 1"],
                observations=[],
                console_errors=[],
                network_findings=[],
                ui_findings=[],
                result="failed",
                evidence_paths=[],
            )
            with self.assertRaises(SystemExit):
                _validate_evidence_paths(report, run_dir, mode="gui", click_steps=1)

    def test_evidence_path_listed_but_missing_file_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            report = OIReport(
                task_id="g4b",
                goal="shell",
                actions=["cmd: ls"],
                observations=[],
                console_errors=[],
                network_findings=[],
                ui_findings=[],
                result="failed",
                evidence_paths=[str(run_dir / "not_exists.txt")],
            )
            with self.assertRaises(SystemExit):
                _validate_evidence_paths(report, run_dir, mode="shell", click_steps=0)

    def test_gui_fails_without_post_click_verify(self) -> None:
        report = OIReport(
            task_id="g5",
            goal='click button "Descargar archivo"',
            actions=["cmd: xdotool search --name Browser", "cmd: xdotool click 1"],
            observations=["step 1 clicked"],
            console_errors=[],
            network_findings=[],
            ui_findings=[],
            result="failed",
            evidence_paths=[],
        )
        with self.assertRaises(SystemExit):
            _validate_gui_post_conditions(
                report,
                mode="gui",
                click_steps=1,
                button_targets={"Descargar archivo"},
            )

    def test_gui_two_steps_verify_only_first_step_is_blocked(self) -> None:
        report = OIReport(
            task_id="g6",
            goal='click button "Descargar archivo"',
            actions=[
                "cmd: xdotool search --name Browser",
                "cmd: xdotool click 1",
                "cmd: xdotool search --name Browser",
                "cmd: xdotool click 1",
            ],
            observations=[
                "step 1 verify visible change after click",
                "step 2 clicked",
            ],
            console_errors=[],
            network_findings=[],
            ui_findings=[],
            result="failed",
            evidence_paths=[],
        )
        with self.assertRaises(SystemExit):
            _validate_gui_post_conditions(
                report,
                mode="gui",
                click_steps=2,
                button_targets={"Descargar archivo"},
            )

    def test_quoted_url_is_not_button_target(self) -> None:
        task = 'haz click en "https://example.com/descargar" cerca del botón principal'
        targets = _extract_button_targets(task)
        self.assertEqual(targets, set())


if __name__ == "__main__":
    unittest.main()
