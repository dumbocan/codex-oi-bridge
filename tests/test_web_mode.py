import sys
import tempfile
import types
import unittest
import json
import os
from pathlib import Path
from unittest.mock import patch

from bridge.cli import _validate_evidence_paths, _validate_report_actions
from bridge.constants import WEB_ALLOWED_COMMAND_PREFIXES
from bridge.models import OIReport
from bridge.web_backend import (
    WebStep,
    _is_relevant_manual_learning_event,
    _learned_selectors_for_step,
    _normalize_learning_target_key,
    _observer_useful_event_count,
    _should_soft_skip_wait_timeout,
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

    def click(self, timeout: int | None = None) -> None:
        if self._text and self._text == self._page.fail_click_text:
            raise RuntimeError("text not found")
        self._page._title = "Demo after click"
        self._page.url = self._page.url + "#clicked"
        self._page._emit("console", _FakeConsoleMessage("console-error"))
        self._page._emit("response", _FakeResponse("GET", "http://localhost:5173/api", 500))
        self._page._emit("requestfailed", _FakeRequest("GET", "http://localhost:5173/asset"))

    def fill(self, value: str, timeout: int | None = None) -> None:
        self._page.filled[self._selector or self._text or "unknown"] = value

    def select_option(self, *, label: str | None = None, value: str | None = None) -> None:
        choice = label or value or ""
        self._page._title = f"Selected {choice}"

    def wait_for(self, state: str = "visible", timeout: int | None = None) -> None:
        if self._text and self._text == self._page.fail_wait_for_text:
            raise TimeoutError("Timeout exceeded while waiting for target")
        if self._selector and self._page.fail_selector_contains:
            if self._page.fail_selector_contains in self._selector:
                raise TimeoutError("Timeout exceeded while waiting for selector")
        self._page.waited_text = self._text

    def is_visible(self, timeout: int | None = None) -> bool:
        if self._text == "Entrar demo":
            return bool(self._page.demo_button_available)
        return True

    def is_enabled(self) -> bool:
        if self._text == "Entrar demo":
            return bool(self._page.demo_button_available)
        return True

    def count(self) -> int:
        if self._text == "Entrar demo":
            return 1 if self._page.demo_button_available else 0
        if self._page.authenticated and self._text in self._page.auth_hints:
            return 1
        return 0

    def bounding_box(self) -> dict[str, float]:
        return {"x": 120.0, "y": 80.0, "width": 20.0, "height": 20.0}

    def scroll_into_view_if_needed(self) -> None:
        return

    def evaluate(self, script: str):
        if "scrollIntoView" in script:
            return None
        if "elementFromPoint" in script:
            return {"x": 130.0, "y": 90.0, "ok": True}
        return None

    def get_by_text(self, text: str, exact: bool = False):
        return _FakeNode(self._page, text=text)


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


class _FakeKeyboard:
    def press(self, _key: str) -> None:
        return


class _FakePage:
    def __init__(
        self,
        *,
        authenticated: bool = False,
        fail_click_text: str = "",
        fail_wait_for_text: str = "",
        demo_button_available: bool = True,
        fail_selector_contains: str = "",
    ):
        self._handlers = {}
        self.url = "about:blank"
        self._title = "Demo"
        self.authenticated = authenticated
        self.fail_click_text = fail_click_text
        self.fail_wait_for_text = fail_wait_for_text
        self.demo_button_available = demo_button_available
        self.fail_selector_contains = fail_selector_contains
        self.main_frame_context_failures = 0
        self._main_frame_context_checks = 0
        self.iframe_focus_locked = False
        self.iframe_pointer_events_disabled = False
        self.waited_selector = ""
        self.waited_text = ""
        self.filled: dict[str, str] = {}
        self.mouse = _FakeMouse(self)
        self.keyboard = _FakeKeyboard()
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
        self.closed = False

    def set_default_timeout(self, _value: int) -> None:
        return

    def is_closed(self) -> bool:
        return bool(self.closed)

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

    def get_by_role(self, role: str, name: str):
        if role == "button":
            return _FakeNode(self, text=name)
        return _FakeNode(self, text="")

    def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
        self.waited_selector = selector

    def wait_for_timeout(self, _ms: int) -> None:
        return

    def add_init_script(self, script: str) -> None:
        self.overlay_installed = "__bridgeShowClick" in script and "__bridgePulseAt" in script
        self.init_scripts.append(script)

    def evaluate(self, _script: str, payload=None):
        self.eval_calls.append((_script, payload))
        if "active.blur" in _script and "IFRAME" in _script:
            self.iframe_focus_locked = False
            return True
        if "data-bridge-prev-pe" in _script and "pointerEvents = 'none'" in _script:
            if self.iframe_focus_locked:
                self.iframe_pointer_events_disabled = True
                return {"idx": 0, "id": "yt-iframe", "prev": ""}
            return None
        if "frame.style.pointerEvents = prev" in _script and "data-bridge-prev-pe" in _script:
            self.iframe_pointer_events_disabled = False
            return True
        if "iframe:focus,iframe:focus-within" in _script:
            return self.iframe_focus_locked
        if "window === window.top" in _script:
            self._main_frame_context_checks += 1
            return self._main_frame_context_checks > self.main_frame_context_failures
        if "window.__bridgeEnsureOverlay" in _script:
            return True
        if "getElementById('__bridge_cursor_overlay')" in _script and "pointerEvents" in _script:
            self._overlay_visible_checks += 1
            visible = self._overlay_visible_checks > self.overlay_visible_after
            return {
                "exists": visible,
                "parent": "body" if visible else "",
                "display": "block" if visible else "none",
                "visibility": "visible" if visible else "hidden",
                "opacity": "1" if visible else "0",
                "z_index": 2147483647 if visible else 0,
                "pointer_events": "none" if visible else "",
            }
        if "window.__bridgeOverlayInstalled = false" in _script:
            self.overlay_installed = False
            return True
        if "window.__bridgeEnsureOverlay?.()" in _script:
            self.overlay_installed = True
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

    def test_parse_steps_supports_fill_selector_text(self) -> None:
        steps = _parse_steps(
            'open http://localhost:5173 type text "ready mix" into selector "#playlist-name-input" '
            'click selector:"#create-playlist-btn"'
        )
        self.assertEqual(steps[0].kind, "fill_selector")
        self.assertEqual(steps[0].target, "#playlist-name-input")
        self.assertEqual(steps[0].value, "ready mix")

    def test_parse_steps_supports_add_all_ready(self) -> None:
        steps = _parse_steps(
            "open http://localhost:5173 add all ready tracks to playlist"
        )
        kinds = [step.kind for step in steps]
        self.assertIn("add_all_ready_to_playlist", kinds)

    def test_parse_steps_supports_remove_all_playlist_tracks(self) -> None:
        steps = _parse_steps(
            "open http://localhost:5173 remove all tracks from playlist"
        )
        kinds = [step.kind for step in steps]
        self.assertIn("remove_all_playlist_tracks", kinds)

    def test_run_web_task_requires_url(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            with self.assertRaises(SystemExit):
                run_web_task("haz click en boton demo", run_dir, 30)

    def test_run_web_task_interactive_hard_timeout_finishes_and_writes_report(self) -> None:
        page = _FakePage(demo_button_available=False)
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r-hard-step"
            run_dir.mkdir(parents=True)
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            status_calls: list[dict] = []
            try:
                with patch("bridge.web_backend._preflight_target_reachable"), patch(
                    "bridge.web_backend._preflight_stack_prereqs"
                ), patch(
                    "bridge.web_backend._playwright_available",
                    return_value=True,
                ), patch(
                    "bridge.web_backend.write_status",
                    side_effect=lambda **kwargs: status_calls.append(dict(kwargs)),
                ), patch(
                    "bridge.web_backend._apply_interactive_step_with_retries",
                    return_value=types.SimpleNamespace(
                        stuck=False,
                        selector_used="",
                        attempted="hard-timeout",
                        deadline_hit=True,
                    ),
                ):
                    report = run_web_task(
                        "open http://localhost:5173, click 'Stop'",
                        run_dir,
                        30,
                        verified=False,
                        visual=True,
                        teaching_mode=True,
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

            report_path = run_dir / "report.json"
            self.assertTrue(report_path.exists())
            saved = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertIn(report.result, {"partial", "failed"})
        self.assertTrue(any("what_failed=interactive_timeout" in i for i in report.ui_findings))
        self.assertTrue(any("final_state=" in i for i in report.ui_findings))
        self.assertEqual(saved["result"], report.result)
        self.assertTrue(any(call.get("state") == "completed" for call in status_calls))
        self.assertTrue(all(call.get("state") != "running" for call in status_calls[-1:]))

    def test_run_web_task_run_timeout_finishes_and_releases_control(self) -> None:
        page = _FakePage(demo_button_available=False)
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module

        def ticking() -> float:
            ticking.t += 1.0
            return ticking.t

        ticking.t = 0.0

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r-hard-run"
            run_dir.mkdir(parents=True)
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            status_calls: list[dict] = []
            try:
                with patch("bridge.web_backend._preflight_target_reachable"), patch(
                    "bridge.web_backend._preflight_stack_prereqs"
                ), patch(
                    "bridge.web_backend._playwright_available",
                    return_value=True,
                ), patch(
                    "bridge.web_backend.write_status",
                    side_effect=lambda **kwargs: status_calls.append(dict(kwargs)),
                ), patch(
                    "bridge.web_backend.time.monotonic",
                    side_effect=ticking,
                ), patch.dict(
                    os.environ,
                    {"BRIDGE_WEB_RUN_HARD_TIMEOUT_SECONDS": "0.1"},
                    clear=False,
                ):
                    report = run_web_task(
                        "open http://localhost:5173, click 'Stop'",
                        run_dir,
                        30,
                        verified=False,
                        visual=True,
                        teaching_mode=True,
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

            report_path = run_dir / "report.json"
            self.assertTrue(report_path.exists())

        self.assertIn(report.result, {"partial", "failed"})
        self.assertTrue(any("what_failed=run_timeout" in i for i in report.ui_findings))
        self.assertTrue(any("control released" in i for i in report.ui_findings))
        self.assertTrue(any("final_state=" in i for i in report.ui_findings))
        self.assertTrue(any(call.get("state") == "completed" for call in status_calls))
        self.assertNotEqual(status_calls[-1].get("state"), "running")

    def test_page_closed_during_step_finishes_with_run_crash_report(self) -> None:
        page = _FakePage(demo_button_available=False)
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r-closed-step"
            run_dir.mkdir(parents=True)
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            try:
                with patch("bridge.web_backend._preflight_target_reachable"), patch(
                    "bridge.web_backend._preflight_stack_prereqs"
                ), patch("bridge.web_backend._playwright_available", return_value=True), patch(
                    "bridge.web_backend._apply_interactive_step_with_retries",
                    side_effect=RuntimeError("Target page, context or browser has been closed"),
                ):
                    report = run_web_task(
                        "open http://localhost:5173, click 'Stop'",
                        run_dir,
                        30,
                        verified=False,
                        visual=True,
                        teaching_mode=True,
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

            report_path = run_dir / "report.json"
            self.assertTrue(report_path.exists())
            saved = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report.result, "failed")
        self.assertEqual(saved["result"], "failed")
        self.assertTrue(any("what_failed=run_crash" in i for i in report.ui_findings))
        self.assertTrue(any("final_state=failed" in i for i in report.ui_findings))

    def test_close_during_handoff_finally_does_not_break_report_persistence(self) -> None:
        page = _FakePage(
            fail_wait_for_text="Stop",
            fail_selector_contains="Stop",
            demo_button_available=False,
        )
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module
        session = WebSession(
            session_id="s-closed-finally",
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

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r-closed-finally"
            run_dir.mkdir(parents=True)
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            try:
                with patch("bridge.web_backend.mark_controlled"), patch(
                    "bridge.web_backend._capture_manual_learning", return_value=None
                ), patch(
                    "bridge.web_backend._show_custom_handoff_notice",
                    side_effect=lambda *_args, **_kwargs: setattr(page, "closed", True),
                ):
                    report = _execute_playwright(
                        "http://localhost:5173",
                        [WebStep("click_text", "Stop")],
                        run_dir,
                        30,
                        verified=False,
                        visual=True,
                        session=session,
                        teaching_mode=True,
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

        self.assertIn(report.result, {"failed", "partial"})
        self.assertTrue(any("what_failed=" in i for i in report.ui_findings))
        self.assertTrue(any("final_state=" in i for i in report.ui_findings))

    def test_web_open_click_select_wait_and_capture(self) -> None:
        page = _FakePage(demo_button_available=False)
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
            self.assertEqual(len(report.evidence_paths), 5)

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
            with patch("bridge.web_backend._preflight_target_reachable"), patch(
                "bridge.web_backend._playwright_available", return_value=False
            ):
                with self.assertRaises(SystemExit) as ctx:
                    run_web_task(
                        "abre http://localhost:5173, y verifica",
                        run_dir,
                        30,
                    )
            self.assertIn("Playwright Python package is not installed", str(ctx.exception))

    def test_demo_step_is_not_auto_duplicated_when_task_already_requests_it(self) -> None:
        page = _FakePage(demo_button_available=True)
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
                    [WebStep("maybe_click_text", "Entrar demo")],
                    run_dir,
                    30,
                    verified=False,
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

        self.assertEqual(sum(1 for action in report.actions if "maybe click text:Entrar demo" in action), 1)
        self.assertTrue(any("skipping auto demo click" in item for item in report.observations))

    def test_interactive_click_timeout_fails_fast(self) -> None:
        page = _FakePage(fail_wait_for_text="Reproducir", demo_button_available=False)
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
                    [WebStep("click_text", "Reproducir")],
                    run_dir,
                    30,
                    verified=False,
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

        self.assertEqual(report.result, "failed")
        self.assertTrue(any("Timeout on interactive step" in item for item in report.console_errors))
        self.assertTrue(any("timeout on click_text:Reproducir" in item for item in report.ui_findings))
        self.assertTrue(any("why_likely=" in item for item in report.ui_findings))
        self.assertTrue(any(path.endswith("_timeout.png") for path in report.evidence_paths))

    def test_click_text_falls_back_to_stable_selector(self) -> None:
        page = _FakePage(fail_wait_for_text="Stop", demo_button_available=False)
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
                    [WebStep("click_text", "Stop")],
                    run_dir,
                    30,
                    verified=False,
                    teaching_mode=True,
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

        self.assertEqual(report.result, "partial")
        self.assertTrue(any("stable selector fallback" in item for item in report.observations))

    def test_click_selector_stop_falls_back_to_stop_text_in_teaching(self) -> None:
        page = _FakePage(fail_selector_contains="#player-stop-btn", demo_button_available=False)
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r-stop-fallback"
            run_dir.mkdir(parents=True)
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            try:
                report = _execute_playwright(
                    "http://localhost:5173",
                    [WebStep("click_selector", "#player-stop-btn")],
                    run_dir,
                    30,
                    verified=False,
                    visual=True,
                    teaching_mode=True,
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

        self.assertIn(report.result, {"success", "partial"})
        self.assertTrue(
            any("cmd: playwright click" in a and "stop" in a.lower() for a in report.actions)
        )

    def test_teaching_mode_releases_control_and_writes_learning_artifact(self) -> None:
        page = _FakePage(
            fail_wait_for_text="Stop",
            fail_selector_contains="Stop",
            demo_button_available=False,
        )
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module
        session = WebSession(
            session_id="s-teach",
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

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            learn_dir = Path(tmp) / "learn"
            learn_json = learn_dir / "selectors.json"
            learned_written = False
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            try:
                with patch("bridge.web_backend.mark_controlled"), patch(
                    "bridge.web_backend._LEARNING_DIR", learn_dir
                ), patch("bridge.web_backend._LEARNING_JSON", learn_json), patch(
                    "bridge.web_backend.request_session_state",
                    return_value={
                        "recent_events": [
                            {
                                "type": "click",
                                "selector": "#transport-stop",
                                "target": "Stop",
                                "url": "http://localhost:5173",
                                "created_at": "2026-02-16T10:00:00+00:00",
                                "message": "click Stop",
                            }
                        ]
                    },
                ):
                    report = _execute_playwright(
                        "http://localhost:5173",
                        [WebStep("click_text", "Stop")],
                        run_dir,
                        30,
                        verified=False,
                        visual=True,
                        session=session,
                        teaching_mode=True,
                    )
                    learned_written = learn_json.exists()
            finally:
                if old_playwright is None:
                    sys.modules.pop("playwright", None)
                else:
                    sys.modules["playwright"] = old_playwright
                if old_sync is None:
                    sys.modules.pop("playwright.sync_api", None)
                else:
                    sys.modules["playwright.sync_api"] = old_sync

        self.assertEqual(report.result, "partial")
        self.assertTrue(any("No encuentro el botón: Stop" in item for item in report.ui_findings))
        self.assertTrue(any("control released" in item for item in report.ui_findings))
        self.assertTrue(any("/learning/teaching_" in path for path in report.evidence_paths))
        self.assertTrue(learned_written)
        self.assertTrue(any("Gracias, ya he aprendido" in item for item in report.ui_findings))

    def test_teaching_mode_stuck_triggers_release_and_human_assist_metadata(self) -> None:
        page = _FakePage(
            fail_wait_for_text="Stop",
            fail_selector_contains="Stop",
            demo_button_available=False,
        )
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module
        session = WebSession(
            session_id="s-stuck",
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

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            try:
                with patch("bridge.web_backend.mark_controlled"), patch(
                    "bridge.web_backend._capture_manual_learning", return_value=None
                ), patch(
                    "bridge.web_backend._apply_interactive_step_with_retries",
                    return_value=types.SimpleNamespace(
                        stuck=True, selector_used="", attempted="retry=0"
                    ),
                ):
                    report = _execute_playwright(
                        "http://localhost:5173",
                        [WebStep("click_text", "Stop")],
                        run_dir,
                        30,
                        verified=False,
                        visual=True,
                        session=session,
                        teaching_mode=True,
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

        self.assertEqual(report.result, "partial")
        self.assertTrue(any("Me he atascado en" in item for item in report.ui_findings))
        self.assertTrue(any("control released" in item for item in report.ui_findings))
        self.assertTrue(any("what_failed=stuck" in item for item in report.ui_findings))
        self.assertTrue(any("next_best_action=human_assist" in item for item in report.ui_findings))
        self.assertTrue(any("teaching handoff: browser kept open" in item for item in report.ui_findings))
        self.assertTrue(any("learning_capture=none" in item for item in report.ui_findings))
        self.assertTrue(any("__bridge_learning_handoff_overlay" in script for script, _ in page.eval_calls))

    def test_teaching_mode_watchdog_stuck_without_exception_triggers_handoff(self) -> None:
        page = _FakePage(demo_button_available=False)
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module
        session = WebSession(
            session_id="s-watchdog",
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

        def ticking() -> float:
            ticking.t += 5.0
            return ticking.t

        ticking.t = 0.0

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            try:
                with patch("bridge.web_backend.mark_controlled"), patch(
                    "bridge.web_backend._apply_interactive_step_with_retries",
                    return_value=types.SimpleNamespace(stuck=False, selector_used="", attempted="noop"),
                ), patch("bridge.web_backend.time.monotonic", side_effect=ticking), patch(
                    "bridge.web_backend._capture_manual_learning", return_value=None
                ), patch.dict(
                    os.environ,
                    {
                        "BRIDGE_WEB_STUCK_INTERACTIVE_SECONDS": "3",
                        "BRIDGE_WEB_STUCK_STEP_SECONDS": "6",
                    },
                    clear=False,
                ):
                    report = _execute_playwright(
                        "http://localhost:5173",
                        [WebStep("click_text", "Stop")],
                        run_dir,
                        30,
                        verified=False,
                        visual=True,
                        session=session,
                        teaching_mode=True,
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

        self.assertEqual(report.result, "partial")
        self.assertTrue(any("what_failed=stuck" in item for item in report.ui_findings))
        self.assertTrue(any("next_best_action=human_assist" in item for item in report.ui_findings))
        self.assertTrue(any("cmd: playwright release control (teaching handoff)" in item for item in report.actions))

    def test_iframe_focus_recovers_to_main_frame_and_continues(self) -> None:
        page = _FakePage(demo_button_available=False)
        page.iframe_focus_locked = True
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
                    [WebStep("click_selector", "#player-stop-btn")],
                    run_dir,
                    30,
                    verified=False,
                    visual=True,
                    teaching_mode=True,
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

        self.assertIn(report.result, {"success", "partial"})
        self.assertFalse(page.iframe_focus_locked)
        self.assertFalse(page.iframe_pointer_events_disabled)
        self.assertTrue(any("cmd: playwright click" in item and "stop" in item.lower() for item in report.actions))
        self.assertFalse(any("iframe" in item.lower() and "click" in item.lower() for item in report.actions))

    def test_iframe_focus_cannot_recover_triggers_handoff(self) -> None:
        page = _FakePage(demo_button_available=False)
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module
        session = WebSession(
            session_id="s-iframe-stuck",
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

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            try:
                with patch("bridge.web_backend.mark_controlled"), patch(
                    "bridge.web_backend._capture_manual_learning", return_value=None
                ), patch(
                    "bridge.web_backend._force_main_frame_context", return_value=False
                ):
                    report = _execute_playwright(
                        "http://localhost:5173",
                        [WebStep("click_text", "Stop")],
                        run_dir,
                        30,
                        verified=False,
                        visual=True,
                        session=session,
                        teaching_mode=True,
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

        self.assertEqual(report.result, "partial")
        self.assertTrue(any("what_failed=stuck_iframe_focus" in item for item in report.ui_findings))
        self.assertTrue(any("Me he quedado dentro de YouTube iframe" in item for item in report.ui_findings))
        self.assertTrue(any("cmd: playwright release control (teaching handoff)" in item for item in report.actions))
        self.assertTrue(any("teaching handoff: browser kept open" in item for item in report.ui_findings))
        self.assertTrue(any("__bridge_user_control_overlay" in script for script, _ in page.eval_calls))
        self.assertFalse(
            any(
                "__bridge_learning_handoff_overlay" in script and payload == [True]
                for script, payload in page.eval_calls
            )
        )

    def test_stuck_manual_learning_is_persisted(self) -> None:
        page = _FakePage(
            fail_wait_for_text="Stop",
            fail_selector_contains="Stop",
            demo_button_available=False,
        )
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module
        session = WebSession(
            session_id="s-stuck-learn",
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

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            learn_dir = Path(tmp) / "learn"
            learn_json = learn_dir / "selectors.json"
            learned_payload = {}
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            t = {"v": 0.0}

            def monotonic_tick() -> float:
                t["v"] += 0.7
                return t["v"]

            try:
                with patch("bridge.web_backend.mark_controlled"), patch(
                    "bridge.web_backend._LEARNING_DIR", learn_dir
                ), patch("bridge.web_backend._LEARNING_JSON", learn_json), patch(
                    "bridge.web_backend.time.monotonic", side_effect=monotonic_tick
                ), patch(
                    "bridge.web_backend._capture_manual_learning",
                    return_value={
                        "failed_target": "Stop",
                        "selector": "#transport-stop",
                        "target": "Stop",
                        "timestamp": "2026-02-16T10:00:00+00:00",
                        "url": "http://localhost:5173",
                        "state_key": "localhost:5173/|demo",
                    },
                ):
                    report = _execute_playwright(
                        "http://localhost:5173",
                        [WebStep("click_text", "Stop")],
                        run_dir,
                        30,
                        verified=False,
                        visual=True,
                        session=session,
                        teaching_mode=True,
                    )
                    if learn_json.exists():
                        learned_payload = json.loads(learn_json.read_text(encoding="utf-8"))
            finally:
                if old_playwright is None:
                    sys.modules.pop("playwright", None)
                else:
                    sys.modules["playwright"] = old_playwright
                if old_sync is None:
                    sys.modules.pop("playwright.sync_api", None)
                else:
                    sys.modules["playwright.sync_api"] = old_sync

        self.assertTrue(learned_payload)
        self.assertIn("localhost:5173/|demo", learned_payload)
        self.assertIn("stop", learned_payload["localhost:5173/|demo"])
        self.assertTrue(any("/learning/teaching_" in path for path in report.evidence_paths))

    def test_next_run_uses_selector_learned_for_same_context(self) -> None:
        page = _FakePage(
            fail_wait_for_text="Stop",
            fail_selector_contains="button:has-text(\"Stop\")",
            demo_button_available=False,
        )
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module

        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            learn_dir = Path(tmp) / "learn"
            learn_dir.mkdir(parents=True, exist_ok=True)
            learn_json = learn_dir / "selectors.json"
            learn_json.write_text(
                json.dumps({"localhost:5173/|demo": {"stop": ["#transport-stop"]}}, ensure_ascii=False),
                encoding="utf-8",
            )
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            try:
                with patch("bridge.web_backend._LEARNING_DIR", learn_dir), patch(
                    "bridge.web_backend._LEARNING_JSON", learn_json
                ):
                    report = _execute_playwright(
                        "http://localhost:5173",
                        [WebStep("click_text", "Stop")],
                        run_dir,
                        30,
                        verified=False,
                        visual=True,
                        teaching_mode=True,
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

        self.assertTrue(any("cmd: playwright click selector:#transport-stop" in a for a in report.actions))

    def test_learning_event_filter_ignores_bridge_controls(self) -> None:
        self.assertFalse(
            _is_relevant_manual_learning_event(
                {"selector": "#__bridge_release_btn", "target": "Release", "text": "Release"},
                "Stop",
            )
        )
        self.assertTrue(
            _is_relevant_manual_learning_event(
                {"selector": "#transport-stop", "target": "Stop", "text": "Stop"},
                "step 2/2 click_text:Stop",
            )
        )

    def test_timeout_handoff_captures_manual_stop_and_persists_stop_key(self) -> None:
        page = _FakePage(demo_button_available=False)
        page._title = "Audio3"
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module
        session = WebSession(
            session_id="s-timeout-learn",
            pid=123,
            port=9222,
            user_data_dir="/tmp/x",
            browser_binary="/usr/bin/chromium",
            url="http://127.0.0.1:5181",
            title="Audio3",
            controlled=True,
            created_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-01-01T00:00:00+00:00",
            state="open",
            control_port=9555,
            agent_pid=201,
        )
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r-timeout-learn"
            run_dir.mkdir(parents=True)
            learn_dir = Path(tmp) / "learn"
            learn_json = learn_dir / "selectors.json"
            old_playwright = sys.modules.get("playwright")
            old_sync = sys.modules.get("playwright.sync_api")
            sys.modules["playwright"] = fake_playwright
            sys.modules["playwright.sync_api"] = fake_sync_module
            try:
                with patch("bridge.web_backend.mark_controlled"), patch(
                    "bridge.web_backend._LEARNING_DIR", learn_dir
                ), patch(
                    "bridge.web_backend._LEARNING_JSON", learn_json
                ), patch(
                    "bridge.web_backend._apply_interactive_step_with_retries",
                    return_value=types.SimpleNamespace(
                        stuck=False,
                        selector_used="",
                        attempted="retry=0, selector=#player-stop-btn",
                        deadline_hit=True,
                    ),
                ), patch(
                    "bridge.web_backend.request_session_state",
                    return_value={
                        "recent_events": [
                            {
                                "type": "click",
                                "selector": "#player-stop-btn",
                                "target": "Stop",
                                "text": "Stop",
                                "url": "http://127.0.0.1:5181",
                                "created_at": "2026-02-18T00:11:00+00:00",
                                "message": "click Stop",
                            }
                        ]
                    },
                ):
                    report = _execute_playwright(
                        "http://127.0.0.1:5181",
                        [WebStep("click_selector", "#player-stop-btn")],
                        run_dir,
                        30,
                        verified=False,
                        visual=True,
                        session=session,
                        teaching_mode=True,
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
            payload = json.loads(learn_json.read_text(encoding="utf-8"))

        self.assertTrue(any("what_failed=interactive_timeout" in item for item in report.ui_findings))
        self.assertFalse(any("learning_capture=none" in item for item in report.ui_findings))
        self.assertIn("127.0.0.1:5181/|audio3", payload)
        self.assertIn("stop", payload["127.0.0.1:5181/|audio3"])
        self.assertIn("#player-stop-btn", payload["127.0.0.1:5181/|audio3"]["stop"])

    def test_learning_key_normalization_avoids_step_signature_garbage(self) -> None:
        self.assertEqual(_normalize_learning_target_key("step 4/5 wait_text:Audio3"), "")
        self.assertEqual(
            _normalize_learning_target_key("step 4/5 click_selector:#player-stop-btn"),
            "stop",
        )

    def test_learned_selectors_lookup_uses_normalized_key(self) -> None:
        step = WebStep("click_selector", "#player-stop-btn")
        selector_map = {"127.0.0.1:5181/|audio3": {"stop": ["#player-stop-btn"]}}
        context = {"state_key": "127.0.0.1:5181/|audio3"}
        learned = _learned_selectors_for_step(step, selector_map, context)
        self.assertEqual(learned, ["#player-stop-btn"])

    def test_soft_skip_wait_timeout_for_now_playing_when_stop_follows(self) -> None:
        steps = [
            WebStep("click_selector", "#track-play-track-stan"),
            WebStep("wait_text", "Now playing:"),
            WebStep("click_selector", "#player-stop-btn"),
        ]
        self.assertTrue(
            _should_soft_skip_wait_timeout(
                steps=steps,
                idx=2,
                step=steps[1],
                teaching_mode=True,
            )
        )
        self.assertFalse(
            _should_soft_skip_wait_timeout(
                steps=steps,
                idx=2,
                step=steps[1],
                teaching_mode=False,
            )
        )

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

    def test_web_actions_ignore_learning_resume_click_for_evidence_count(self) -> None:
        report = OIReport(
            task_id="r1",
            goal="web",
            actions=[
                "cmd: playwright goto http://localhost:5173",
                "cmd: playwright click text:Entrar demo",
                "cmd: playwright click selector:#track-play-track-stan (learning-resume)",
            ],
            observations=["Opened URL", "Clicked text in step 1"],
            console_errors=[],
            network_findings=[],
            ui_findings=["step 1 verify visible result"],
            result="partial",
            evidence_paths=[],
        )
        click_steps = _validate_report_actions(
            report,
            confirm_sensitive=True,
            expected_targets={"http://localhost:5173"},
            allowlist=WEB_ALLOWED_COMMAND_PREFIXES,
            mode="web",
        )
        self.assertEqual(click_steps, 1)

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

    def test_visual_mode_does_not_abort_when_overlay_is_not_visible(self) -> None:
        page = _FakePage()
        page.overlay_visible_after = 999
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
        self.assertTrue(
            any("visual overlay degraded" in item.lower() for item in report.ui_findings)
        )
        self.assertIn(report.result, ("success", "partial"))

    def test_visual_attach_renders_overlay_visible(self) -> None:
        page = _FakePage()
        fake_sync_module = types.ModuleType("playwright.sync_api")
        fake_sync_module.sync_playwright = lambda: _FakePlaywrightCtx(page)
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_module
        session = WebSession(
            session_id="s-attach",
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
                        visual=True,
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

        self.assertFalse(any("visual overlay degraded" in item for item in report.ui_findings))
        self.assertTrue(any("window.__bridgeOverlayInstalled = false" in s for s, _ in page.eval_calls))

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
        self.assertIn("manual click captured", script)
        self.assertIn("type: 'mousemove'", script)
        self.assertIn("type: 'scroll'", script)
        self.assertIn("observer_noise_mode", script)

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
        self.assertIn("USER CONTROL", script)
        self.assertIn("LEARNING/HANDOFF", script)
        self.assertIn("network_warn", script)
        self.assertIn("READY FOR MANUAL TEST", script)
        self.assertIn("readyManual", script)
        self.assertIn('aria-label="session-ready-manual-test"', script)
        self.assertIn("background:#16a34a", script)
        self.assertIn("border:1px solid #22c55e", script)
        self.assertIn(">● READY FOR MANUAL TEST</span>", script)

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
        self.assertIn("rgba(245,158,11,0.24)", script)
        self.assertIn("rgba(22,163,74,0.22)", script)
        self.assertIn("rgba(34,197,94,0.95)", script)

        # Priority: controlled (blue) > incident (red) > ready (green)
        bg_idx = script.find("bar.style.background")
        ctrl_idx = script.find("controlled", bg_idx)
        inc_idx = script.find("incidentOpen", bg_idx)
        ready_idx = script.find("readyManual", bg_idx)
        self.assertNotEqual(bg_idx, -1)
        self.assertTrue(ctrl_idx != -1 and inc_idx != -1 and ready_idx != -1)
        self.assertLess(ctrl_idx, inc_idx)
        self.assertLess(inc_idx, ready_idx)

    def test_observer_useful_events_ignore_trivial_in_minimal(self) -> None:
        session = WebSession(
            session_id="s-noise",
            pid=1,
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
        with patch(
            "bridge.web_backend.request_session_state",
            return_value={
                "observer_noise_mode": "minimal",
                "recent_events": [
                    {"type": "mousemove"},
                    {"type": "scroll"},
                    {"type": "click"},
                    {"type": "console_error"},
                ],
            },
        ):
            self.assertEqual(_observer_useful_event_count(session), 2)

    def test_observer_useful_events_include_scroll_move_in_debug(self) -> None:
        session = WebSession(
            session_id="s-noise-debug",
            pid=1,
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
        with patch(
            "bridge.web_backend.request_session_state",
            return_value={
                "observer_noise_mode": "debug",
                "recent_events": [
                    {"type": "mousemove"},
                    {"type": "scroll"},
                    {"type": "click"},
                    {"type": "console_error"},
                ],
            },
        ):
            self.assertEqual(_observer_useful_event_count(session), 4)

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
