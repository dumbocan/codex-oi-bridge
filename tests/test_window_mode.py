import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.window_backend import run_window_task, should_handle_window_task


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return type("Proc", (), {"stdout": stdout, "stderr": stderr, "returncode": returncode})()


class WindowModeTests(unittest.TestCase):
    def test_should_handle_window_task(self) -> None:
        self.assertTrue(should_handle_window_task("window:list window:active"))
        self.assertTrue(should_handle_window_task("listar ventanas y ventana activa"))
        self.assertFalse(should_handle_window_task("solo inspecciona red"))

    def test_window_list_and_active(self) -> None:
        def fake_run(cmd: list[str], timeout_seconds: int):
            if cmd[:2] == ["wmctrl", "-l"]:
                return _fake_completed("0x001 Demo\n")
            if cmd[:3] == ["xdotool", "getactivewindow", "getwindowname"]:
                return _fake_completed("Demo")
            return _fake_completed()

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            with patch("bridge.window_backend._run_cmd", side_effect=fake_run), patch(
                "bridge.window_backend._capture_screenshot",
                side_effect=lambda path, timeout_seconds, console_errors: path.write_bytes(b"png"),
            ):
                report = run_window_task("window:list window:active", run_dir, 30)

        self.assertIn("cmd: wmctrl -l", report.actions)
        self.assertIn("cmd: xdotool getactivewindow getwindowname", report.actions)
        self.assertEqual(report.console_errors, [])
        self.assertTrue(any("verify" in item for item in report.ui_findings))

    def test_window_activate_by_title(self) -> None:
        def fake_run(cmd: list[str], timeout_seconds: int):
            if cmd[:2] == ["wmctrl", "-l"]:
                return _fake_completed("0x001 Demo Browser\n0x002 Other\n")
            if cmd[:2] == ["wmctrl", "-ia"]:
                return _fake_completed()
            return _fake_completed()

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            with patch("bridge.window_backend._run_cmd", side_effect=fake_run), patch(
                "bridge.window_backend._capture_screenshot",
                side_effect=lambda path, timeout_seconds, console_errors: path.write_bytes(b"png"),
            ):
                report = run_window_task('window:activate "Demo Browser"', run_dir, 30)

        self.assertTrue(any("wmctrl -ia" in action for action in report.actions))
        self.assertEqual(report.result, "success")

    def test_window_open_url_when_missing_target(self) -> None:
        def fake_run(cmd: list[str], timeout_seconds: int):
            if cmd[0] == "xdg-open":
                return _fake_completed()
            return _fake_completed()

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            with patch("bridge.window_backend._run_cmd", side_effect=fake_run), patch(
                "bridge.window_backend._capture_screenshot",
                side_effect=lambda path, timeout_seconds, console_errors: path.write_bytes(b"png"),
            ), patch(
                "bridge.window_backend.shutil.which",
                side_effect=lambda cmd: "/usr/bin/xdg-open" if cmd == "xdg-open" else None,
            ):
                report = run_window_task(
                    'window:open "http://localhost:5173"',
                    run_dir,
                    30,
                )

        self.assertTrue(any(action.startswith("cmd: xdg-open") for action in report.actions))
        self.assertEqual(report.result, "success")


if __name__ == "__main__":
    unittest.main()
