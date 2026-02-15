import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.cli import _validate_evidence_paths, _validate_report_actions
from bridge.constants import WEB_ALLOWED_COMMAND_PREFIXES
from bridge.models import OIReport
from bridge.web_backend import (
    WebStep,
    _ensure_visual_overlay_ready,
    _execute_playwright,
    _install_visual_overlay,
    ensure_session_top_bar,
    _session_state_payload,
    _parse_steps,
    run_web_task,
)
from bridge.web_session import WebSession


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


class _FakeNode:
    def __init__(self, page, text: str = "", selector: str = ""):
        self.first = self
        self._page = page
        self._text = text
        self._selector = selector

    def click(self) -> None:
        if self._text and self._text == self._page.fail_click_text:
            raise RuntimeError("text not found")
        self._page._title = "Demo after click"
        self._page.url = self._page.url + "#clicked"
        self._page._emit("console", _FakeConsoleMessage("console-error"))
        self._page._emit("response", _FakeResponse("GET", "http://localhost:5173/api", 500))
        self._page._emit("requestfailed", _FakeRequest("GET", "http://localhost:5173/asset"))

    def select_option(self, *, label: str | None = None, value: str | None = None) -> None:
        choice = label or value or ""
        self._page._title = f"Selected {choice}"

    def wait_for(self, state: str = "visible") -> None:
        self._page.waited_text = self._text

    def count(self) -> int:
        if self._page.authenticated and self._text in self._page.auth_hints:
            return 1
        return 0

    def bounding_box(self) -> dict[str, float]:
        return {"x": 120.0, "y": 80.0, "width": 20.0, "height": 20.0}


class _FakeMouse:
    def __init__(self, page):
        self._page = page
        self.moves: list[tuple[float, float, int]] = []
        self.down_count = 0
        self.up_count = 0

    def move(self, x: float, y: float, steps: int = 1) -> None:
        self.moves.append((x, y, steps))

    def down(self) -> None:
        self.down_count += 1

    def up(self) -> None:
        self.up_count += 1
        self._page._title = "Demo after click"
        self._page.url = self._page.url + "#clicked"
        self._page._emit("console", _FakeConsoleMessage("console-error"))
        self._page._emit("response", _FakeResponse("GET", "http://localhost:5173/api", 500))
        self._page._emit("requestfailed", _FakeRequest("GET", "http://localhost:5173/asset"))


class _FakePage:
    def __init__(self, *, authenticated: bool = False, fail_click_text: str = ""):
        self._handlers = {}
        self.url = "about:blank"
        self._title = "Demo"
        self.authenticated = authenticated
        self.fail_click_text = fail_click_text
        self.waited_selector = ""
        self.waited_text = ""
        self.mouse = _FakeMouse(self)
        self.overlay_installed = False
        self.overlay_events: list[tuple[float, float, str]] = []
        self.pulse_events: list[tuple[float, float]] = []
        self.brought_to_front = False
        self.init_scripts: list[str] = []
        self.eval_calls: list[tuple[str, object]] = []
        self.overlay_visible_after = 0
        self._overlay_visible_checks = 0
        self.auth_hints = {
            "cerrar sesion",
            "cerrar sesión",
            "logout",
            "sign out",
            "dashboard",
            "mi cuenta",
            "perfil",
        }

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
        return _FakeNode(self, selector=selector)

    def get_by_text(self, text: str, exact: bool):
        return _FakeNode(self, text=text)

    def wait_for_selector(self, selector: str) -> None:
        self.waited_selector = selector

    def wait_for_timeout(self, _ms: int) -> None:
        return

    def add_init_script(self, script: str) -> None:
        self.overlay_installed = "__bridgeShowClick" in script and "__bridgePulseAt" in script
        self.init_scripts.append(script)

    def evaluate(self, _script: str, payload=None):
        self.eval_calls.append((_script, payload))
        if "window.__bridgeEnsureOverlay" in _script:
            return True
        if "getElementById('__bridge_cursor_overlay')" in _script:
            self._overlay_visible_checks += 1
            return self._overlay_visible_checks > self.overlay_visible_after
        if "__bridgeShowClick" in _script:
            x, y, label = payload
            self.overlay_events.append((x, y, label))
            return
        if "__bridgePulseAt" in _script:
            x, y = payload
            self.pulse_events.append((x, y))
            return
        if "__bridgeMoveCursor" in _script:
            x, y = payload
            self.overlay_events.append((x, y, "move"))
            return

    def bring_to_front(self) -> None:
        self.brought_to_front = True


class _FakeBrowser:
    def __init__(self, page: _FakePage):
        self._page = page
        self.contexts = [types.SimpleNamespace(pages=[page], new_page=lambda: page)]

    def new_page(self):
        return self._page

    def close(self) -> None:
        return


class _FakeChromium:
    def __init__(self, page: _FakePage):
        self._page = page
        self.launch_calls: list[dict[str, object]] = []

    def launch(self, **kwargs):
        self.launch_calls.append(kwargs)
        return _FakeBrowser(self._page)

    def connect_over_cdp(self, endpoint: str):
        return _FakeBrowser(self._page)


class _FakePlaywright:
    def __init__(self, page: _FakePage):
        self.chromium = _FakeChromium(page)


class _FakePlaywrightCtx:
    def __init__(self, page: _FakePage):
        self._page = page

    def __enter__(self):
        return _FakePlaywright(self._page)

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class WebModeTests(unittest.TestCase):
    def test_parse_steps_supports_wait_click_and_select(self) -> None:
        steps = _parse_steps(
            'abre http://localhost:5173 wait selector:"#ready" click selector:"#go" '
            'select option "ES" from selector "#lang" wait text:"Bienvenido"'
        )
        kinds = [step.kind for step in steps]
        self.assertIn("wait_selector", kinds)
        self.assertIn("click_selector", kinds)
        self.assertIn("select_label", kinds)
        self.assertIn("wait_text", kinds)

    def test_parse_steps_does_not_convert_wait_selector_into_click_selector(self) -> None:
        steps = _parse_steps(
            'abre http://localhost:5173 click en "Entrar demo" wait selector:"#dashboard"'
        )
        kinds = [step.kind for step in steps]
        self.assertIn("click_text", kinds)
        self.assertIn("wait_selector", kinds)
        self.assertNotIn("click_selector", kinds)

    def test_run_web_task_requires_url(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            with self.assertRaises(SystemExit):
                run_web_task("haz click en boton demo", run_dir, 30)

    def test_web_open_click_select_wait_and_capture(self) -> None:
        page = _FakePage()
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
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
                    [
                        WebStep("wait_selector", "#ready"),
                        WebStep("click_selector", "#go"),
                        WebStep("select_label", "#lang", "ES"),
                        WebStep("wait_text", "Bienvenido"),
                    ],
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
            self.assertIn("cmd: playwright click selector:#go", report.actions)
            self.assertIn("cmd: playwright select selector:#lang label:ES", report.actions)
            self.assertIn("cmd: playwright wait selector:#ready", report.actions)
            self.assertIn("cmd: playwright wait text:Bienvenido", report.actions)
            self.assertTrue(any("step 1 verify visible result" in item for item in report.ui_findings))
            self.assertTrue(any("step 2 verify visible result" in item for item in report.ui_findings))
            self.assertEqual(page.waited_selector, "#ready")
            self.assertEqual(page.waited_text, "Bienvenido")
            self.assertEqual(len(report.evidence_paths), 4)

    def test_web_auth_fallback_when_login_button_missing(self) -> None:
        page = _FakePage(authenticated=True, fail_click_text="Entrar demo")
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
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
                    [WebStep("click_text", "Entrar demo")],
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

        self.assertTrue(any("authenticated state detected" in item for item in report.observations))
        self.assertTrue(any("authenticated session already active" in item for item in report.ui_findings))

    def test_web_task_url_with_trailing_comma_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            with patch("bridge.web_backend._playwright_available", return_value=False):
                with self.assertRaises(SystemExit) as ctx:
                    run_web_task(
                        "abre http://localhost:5173, y verifica",
                        run_dir,
                        30,
                    )
            self.assertIn("Playwright Python package is not installed", str(ctx.exception))

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
                    "cmd: playwright wait text:Bienvenido",
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

    def test_visual_mode_runs_headed_with_overlay(self) -> None:
        page = _FakePage()
        fake_sync_module = types.ModuleType("playwright.sync_api")
        ctx = _FakePlaywrightCtx(page)
        fake_sync_module.sync_playwright = lambda: ctx
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
                    [WebStep("click_selector", "#go")],
                    run_dir,
                    30,
                    verified=True,
                    visual=True,
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

        self.assertIn("cmd: playwright visual on", report.actions)
        self.assertTrue(page.overlay_installed)
        self.assertTrue(page.brought_to_front)
        self.assertTrue(page.overlay_events)
        self.assertTrue(page.pulse_events)
        self.assertGreater(page.mouse.down_count, 0)
        self.assertGreater(page.mouse.up_count, 0)

    def test_headless_mode_does_not_enable_overlay_action(self) -> None:
        page = _FakePage()
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
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
                    [WebStep("click_selector", "#go")],
                    run_dir,
                    30,
                    verified=True,
                    visual=False,
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

        self.assertNotIn("cmd: playwright visual on", report.actions)
        self.assertFalse(page.overlay_installed)

    def test_overlay_installed_flag_without_dom_does_not_block_real_install(self) -> None:
        page = _FakePage()
        _install_visual_overlay(
            page,
            cursor_enabled=True,
            click_pulse_enabled=True,
            scale=1.0,
            color="#3BA7FF",
            trace_enabled=True,
        )
        self.assertTrue(page.init_scripts)
        script = page.init_scripts[-1]
        root_idx = script.find("const root = document.documentElement")
        flag_idx = script.find("window.__bridgeOverlayInstalled = true")
        self.assertNotEqual(root_idx, -1)
        self.assertNotEqual(flag_idx, -1)
        self.assertGreater(flag_idx, root_idx)
        self.assertIn("window.__bridgeEnsureOverlay = () => installOverlay()", script)

    def test_top_bar_uses_persistent_agent_channel(self) -> None:
        page = _FakePage()
        _install_visual_overlay(
            page,
            cursor_enabled=True,
            click_pulse_enabled=True,
            scale=1.0,
            color="#3BA7FF",
            trace_enabled=True,
            session_state={
                "session_id": "s1",
                "state": "open",
                "controlled": True,
                "control_url": "http://127.0.0.1:9555",
            },
        )
        script = page.init_scripts[-1]
        self.assertIn("window.__bridgeControlRequest = async", script)
        self.assertIn("fetch(`${controlUrl}/action`", script)
        self.assertNotIn("__bridgeSessionAction?.", script)
        self.assertIn("Clear incident", script)
        self.assertIn("wire(ackBtn, 'ack')", script)

    def test_top_bar_includes_slide_transition_and_offline_label(self) -> None:
        page = _FakePage()
        _install_visual_overlay(
            page,
            cursor_enabled=True,
            click_pulse_enabled=True,
            scale=1.0,
            color="#3BA7FF",
            trace_enabled=True,
            session_state={"session_id": "s1"},
        )
        script = page.init_scripts[-1]
        self.assertIn("translateY(-110%)", script)
        self.assertIn("transform 210ms ease-out", script)
        self.assertIn("agent offline", script)
        self.assertIn("network_warn", script)
        self.assertIn("READY FOR MANUAL TEST", script)
        self.assertIn("readyManual", script)

    def test_top_bar_semantics_keep_blue_red_gray_and_add_green_ready(self) -> None:
        page = _FakePage()
        _install_visual_overlay(
            page,
            cursor_enabled=False,
            click_pulse_enabled=False,
            scale=1.0,
            color="#3BA7FF",
            trace_enabled=False,
            session_state={"session_id": "s1"},
        )
        script = page.init_scripts[-1]
        self.assertIn("controlled", script)
        self.assertIn("incidentOpen", script)
        self.assertIn("readyManual", script)
        self.assertIn("rgba(59,167,255,0.22)", script)
        self.assertIn("rgba(255,82,82,0.26)", script)
        self.assertIn("rgba(70,189,120,0.24)", script)

    def test_web_open_can_inject_top_bar_without_web_run(self) -> None:
        page = _FakePage()
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module
        session = WebSession(
            session_id="s-open",
            pid=123,
            port=9222,
            user_data_dir="/tmp/x",
            browser_binary="/usr/bin/chromium",
            url="http://localhost:5173",
            title="Audio3",
            controlled=False,
            created_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-01-01T00:00:00+00:00",
            state="open",
            control_port=9555,
            agent_pid=201,
        )
        old_playwright = sys.modules.get("playwright")
        old_sync = sys.modules.get("playwright.sync_api")
        sys.modules["playwright"] = fake_playwright
        sys.modules["playwright.sync_api"] = fake_sync_module
        try:
            ensure_session_top_bar(session)
        finally:
            if old_playwright is None:
                sys.modules.pop("playwright", None)
            else:
                sys.modules["playwright"] = old_playwright
            if old_sync is None:
                sys.modules.pop("playwright.sync_api", None)
            else:
                sys.modules["playwright.sync_api"] = old_sync
        self.assertTrue(page.init_scripts)

    def test_overlay_ready_retries_until_visible(self) -> None:
        page = _FakePage()
        page.overlay_visible_after = 2
        _ensure_visual_overlay_ready(page, retries=5, delay_ms=1)
        self.assertGreaterEqual(page._overlay_visible_checks, 3)

    def test_session_state_payload_includes_control_channel(self) -> None:
        session = WebSession(
            session_id="s1",
            pid=123,
            port=9222,
            user_data_dir="/tmp/x",
            browser_binary="/usr/bin/chromium",
            url="http://localhost:5173",
            title="Audio3",
            controlled=True,
            created_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-01-01T00:00:00+00:00",
            state="open",
            control_port=9555,
            agent_pid=201,
        )
        payload = _session_state_payload(session)
        self.assertEqual(payload["control_port"], 9555)
        self.assertEqual(payload["control_url"], "http://127.0.0.1:9555")
        self.assertTrue(payload["agent_online"])

    def test_attach_session_skips_navigation_when_already_at_target(self) -> None:
        page = _FakePage()
        page.url = "http://localhost:5173/"
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module
        session = WebSession(
            session_id="s1",
            pid=123,
            port=9222,
            user_data_dir="/tmp/x",
            browser_binary="/usr/bin/chromium",
            url=page.url,
            title="Audio3",
            controlled=False,
            created_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-01-01T00:00:00+00:00",
            state="open",
        )

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            try:
                with patch("bridge.web_backend.mark_controlled"):
                    report = _execute_playwright(
                        "http://localhost:5173",
                        [WebStep("wait_text", "Audio3")],
                        run_dir,
                        30,
                        verified=False,
                        session=session,
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

        self.assertFalse(any(action.startswith("cmd: playwright goto") for action in report.actions))
        self.assertTrue(any("navigation skipped" in item.lower() for item in report.observations))


if __name__ == "__main__":
    unittest.main()
