import sys
import tempfile
import types
import unittest
from pathlib import Path

from bridge.cli import _validate_evidence_paths, _validate_report_actions
from bridge.constants import WEB_ALLOWED_COMMAND_PREFIXES
from bridge.models import OIReport
from bridge.web_backend import _execute_playwright, run_web_task


class _FakeConsoleMessage:
    def __init__(self, text: str):
        self.type = "error"
        self.text = text


class _FakeRequest:
    def __init__(self, method: str, url: str):
        self.method = method
        self.url = url
        self.failure = {"errorText": "net::ERR_FAILED"}


class _FakeResponse:
    def __init__(self, method: str, url: str, status: int):
        self.status = status
        self.url = url
        self.request = types.SimpleNamespace(method=method)


class _FakeClicker:
    def __init__(self, page):
        self.first = self
        self._page = page

    def click(self) -> None:
        self._page._title = "Demo after click"
        self._page.url = self._page.url + "#clicked"
        self._page._emit("console", _FakeConsoleMessage("console-error"))
        self._page._emit("response", _FakeResponse("GET", "http://localhost:5173/api", 500))
        self._page._emit("requestfailed", _FakeRequest("GET", "http://localhost:5173/asset"))


class _FakePage:
    def __init__(self):
        self._handlers = {}
        self.url = "about:blank"
        self._title = "Demo"

    def set_default_timeout(self, _value: int) -> None:
        return

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def _emit(self, event: str, payload) -> None:
        for handler in self._handlers.get(event, []):
            handler(payload)

    def goto(self, url: str, wait_until: str) -> None:
        self.url = url

    def title(self) -> str:
        return self._title

    def screenshot(self, path: str, full_page: bool) -> None:
        Path(path).write_bytes(b"png")

    def locator(self, selector: str):
        return _FakeClicker(self)

    def get_by_text(self, text: str, exact: bool):
        return _FakeClicker(self)

    def wait_for_timeout(self, _ms: int) -> None:
        return


class _FakeBrowser:
    def __init__(self):
        self._page = _FakePage()

    def new_page(self):
        return self._page

    def close(self) -> None:
        return


class _FakeChromium:
    def launch(self, headless: bool, channel: str | None = None):
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()


class _FakePlaywrightCtx:
    def __enter__(self):
        return _FakePlaywright()

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class WebModeTests(unittest.TestCase):
    def test_run_web_task_requires_url(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            with self.assertRaises(SystemExit):
                run_web_task("haz click en boton demo", run_dir, 30)

    def test_web_open_click_verify_screenshots_and_console_network_capture(self) -> None:
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx()
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            try:
                report = _execute_playwright(
                    "http://localhost:5173",
                    [{"kind": "text", "value": "Entrar demo"}],
                    run_dir,
                    30,
                    verified=True,
                )
            finally:
                if old_playwright is None:
                    sys.modules.pop("playwright", None)
                else:
                    sys.modules["playwright"] = old_playwright
                if old_sync is None:
                    sys.modules.pop("playwright.sync_api", None)
                else:
                    sys.modules["playwright.sync_api"] = old_sync

            self.assertIn("cmd: playwright goto http://localhost:5173", report.actions)
            self.assertIn("cmd: playwright click text:Entrar demo", report.actions)
            self.assertTrue(any("Page title:" in item for item in report.observations))
            self.assertTrue(any("step 1 verify visible result" in item for item in report.ui_findings))
            self.assertTrue(any("console-error" in item for item in report.console_errors))
            self.assertTrue(any("500" in item for item in report.network_findings))
            for rel_path in report.evidence_paths:
                abs_path = Path.cwd() / rel_path
                self.assertTrue(abs_path.exists())
                self.assertGreater(abs_path.stat().st_size, 0)

    def test_web_actions_and_evidence_validations(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            evidence = run_dir / "evidence"
            evidence.mkdir(parents=True)
            before = evidence / "step_1_before.png"
            after = evidence / "step_1_after.png"
            before.write_bytes(b"png")
            after.write_bytes(b"png")
            report = OIReport(
                task_id="r1",
                goal="web",
                actions=[
                    "cmd: playwright goto http://localhost:5173",
                    "cmd: playwright click text:Entrar demo",
                ],
                observations=["Opened URL", "Clicked text in step 1"],
                console_errors=[],
                network_findings=[],
                ui_findings=["step 1 verify visible result"],
                result="success",
                evidence_paths=[
                    str(before.resolve().relative_to(Path.cwd())),
                    str(after.resolve().relative_to(Path.cwd())),
                ],
            )
            click_steps = _validate_report_actions(
                report,
                confirm_sensitive=True,
                expected_targets={"http://localhost:5173"},
                allowlist=WEB_ALLOWED_COMMAND_PREFIXES,
                mode="web",
            )
            safe = _validate_evidence_paths(
                report,
                run_dir,
                mode="web",
                click_steps=click_steps,
                run_id="r1",
            )
            self.assertEqual(click_steps, 1)
            self.assertEqual(len(safe), 2)


if __name__ == "__main__":
    unittest.main()
