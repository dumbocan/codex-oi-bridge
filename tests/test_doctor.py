import unittest
from unittest.mock import patch

from bridge.cli import _collect_runtime_checks


class DoctorTests(unittest.TestCase):
    def test_collect_runtime_checks_shell(self) -> None:
        with patch("bridge.cli._can_resolve", return_value=True), patch(
            "bridge.cli.shutil.which", return_value="/usr/bin/interpreter"
        ):
            checks = _collect_runtime_checks("shell")
        names = {item["name"] for item in checks}
        self.assertIn("openai_api_key", names)
        self.assertIn("dns_api_openai", names)
        self.assertIn("interpreter_binary", names)

    def test_collect_runtime_checks_gui_includes_display_and_tools(self) -> None:
        with patch("bridge.cli._can_resolve", return_value=True), patch(
            "bridge.cli.shutil.which",
            side_effect=lambda cmd: "/usr/bin/" + cmd,
        ), patch(
            "bridge.cli._doctor_screenshot_runtime_check",
            return_value=(True, "ok"),
        ), patch.dict("os.environ", {"DISPLAY": ":0"}, clear=False):
            checks = _collect_runtime_checks("gui")
        names = {item["name"] for item in checks}
        self.assertIn("display_env", names)
        self.assertIn("tool_xdotool", names)
        self.assertIn("tool_wmctrl", names)
        self.assertIn("tool_xwininfo", names)
        self.assertIn("tool_screenshot", names)
        self.assertIn("screenshot_runtime", names)

    def test_collect_runtime_checks_gui_screenshot_runtime_failure(self) -> None:
        with patch("bridge.cli._can_resolve", return_value=True), patch(
            "bridge.cli.shutil.which",
            side_effect=lambda cmd: "/usr/bin/" + cmd,
        ), patch(
            "bridge.cli._doctor_screenshot_runtime_check",
            return_value=(False, "failed"),
        ), patch.dict("os.environ", {"DISPLAY": ":0"}, clear=False):
            checks = _collect_runtime_checks("gui")
        screenshot_check = next(item for item in checks if item["name"] == "screenshot_runtime")
        self.assertFalse(screenshot_check["ok"])

    def test_collect_runtime_checks_web(self) -> None:
        with patch("bridge.cli._playwright_module_available", return_value=True), patch(
            "bridge.cli._web_browser_binary_available",
            return_value=True,
        ):
            checks = _collect_runtime_checks("web")
        names = {item["name"] for item in checks}
        self.assertIn("playwright_python", names)
        self.assertIn("web_browser_binary", names)
        self.assertNotIn("openai_api_key", names)


if __name__ == "__main__":
    unittest.main()
