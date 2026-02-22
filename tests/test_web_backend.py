import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.web_backend import _highlight_target, _preflight_target_reachable
from bridge.web_learning_store import (
    learned_scroll_hints_for_step,
    load_learned_scroll_hints,
    store_learned_scroll_hints,
)
from bridge.web_steps import WebStep
from bridge.web_teaching import capture_manual_learning


class _FakePage:
    def __init__(self):
        self.scroll_calls = 0
        self.wait_calls = 0

    def evaluate(self, script: str, payload=None):
        if "scrollBy(0, -220)" in script:
            self.scroll_calls += 1
        return None

    def wait_for_timeout(self, _ms: int) -> None:
        self.wait_calls += 1
        return


class _FakeLocator:
    def __init__(self, ok_after: int | None = None):
        self.ok_after = ok_after
        self.calls = 0

    def scroll_into_view_if_needed(self) -> None:
        return

    def evaluate(self, script: str):
        self.calls += 1
        if "scrollIntoView" in script:
            return None
        if "elementFromPoint" in script:
            if self.ok_after is not None and self.calls >= self.ok_after:
                return {"x": 10.0, "y": 10.0, "ok": True}
            return {"x": 10.0, "y": 10.0, "ok": False}
        return None


class WebBackendPreflightTests(unittest.TestCase):
    def test_preflight_fails_fast_when_port_not_listening(self) -> None:
        with patch("bridge.web_backend.socket.create_connection", side_effect=OSError("refused")):
            with self.assertRaises(SystemExit):
                _preflight_target_reachable("http://127.0.0.1:65500/")


class WebBackendOcclusionTests(unittest.TestCase):
    def test_occluded_target_retries_scroll_and_returns_none(self) -> None:
        page = _FakePage()
        locator = _FakeLocator(ok_after=None)
        pt = _highlight_target(page, locator, "step 1", click_pulse_enabled=False)
        self.assertIsNone(pt)
        self.assertGreaterEqual(page.scroll_calls, 1)

    def test_target_becomes_clickable_after_retry(self) -> None:
        page = _FakePage()
        locator = _FakeLocator(ok_after=3)
        pt = _highlight_target(page, locator, "step 1", click_pulse_enabled=False)
        self.assertIsNotNone(pt)


class WebTeachingScrollLearningTests(unittest.TestCase):
    def test_capture_manual_learning_includes_recent_scrolls(self) -> None:
        events = [
            {"type": "scroll", "created_at": "t1", "scroll_y": 120, "url": "http://x"},
            {"type": "scroll", "created_at": "t2", "scroll_y": 360, "url": "http://x"},
            {
                "type": "click",
                "created_at": "t3",
                "selector": "#player-stop-btn",
                "target": "Stop",
                "text": "Stop",
                "url": "http://x",
                "message": "click Stop",
            },
        ]

        payload = capture_manual_learning(
            page=None,
            session=object(),
            failed_target="#player-stop-btn",
            context={"state_key": "localhost/|audio3"},
            wait_seconds=5,
            request_session_state=lambda _s: {"recent_events": events},
            show_wrong_click_notice=lambda _page, _t: None,
        )
        self.assertIsNotNone(payload)
        scrolls = list(payload.get("scroll_events", []))
        self.assertEqual([s.get("scroll_y") for s in scrolls], [120, 360])

    def test_store_and_load_scroll_hints_by_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            learning_json = root / "web_teaching_scroll_hints.json"
            ctx = {"state_key": "127.0.0.1:5181/|audio3"}
            store_learned_scroll_hints(
                learning_dir=root,
                learning_json=learning_json,
                target="#track-play-track-stan",
                scroll_positions=[220, 480, 480],
                context=ctx,
                normalize_failed_target_label=lambda raw: str(raw).split(":")[-1].strip(),
            )
            loaded = load_learned_scroll_hints(learning_json)
            hints = learned_scroll_hints_for_step(
                step=WebStep("click_selector", "#track-play-track-stan"),
                scroll_map=loaded,
                context=ctx,
                normalize_failed_target_label=lambda raw: str(raw).split(":")[-1].strip(),
            )
            self.assertEqual(hints, [220, 480])


if __name__ == "__main__":
    unittest.main()
