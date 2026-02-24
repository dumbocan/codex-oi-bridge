import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.web_backend import _highlight_target, _preflight_target_reachable
from bridge.web_handoff_actions import target_not_found_handoff
from bridge.web_interaction_executor import apply_interactive_step
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


class _ExecutorFakePage:
    url = "http://localhost:5181/"

    def locator(self, _selector: str):
        raise AssertionError("locator should not be called when bulk scan returns no selectors")


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


class WebInteractionExecutorHardeningTests(unittest.TestCase):
    def test_bulk_click_in_cards_raises_when_no_clicks_happen(self) -> None:
        page = _ExecutorFakePage()
        actions: list[str] = []
        observations: list[str] = []
        ui_findings: list[str] = []

        with self.assertRaises(SystemExit) as ctx:
            apply_interactive_step(
                page=page,
                step=WebStep("bulk_click_in_cards", "[id^=track-play-local-]", ".track-card||Virginia Beach"),
                step_num=5,
                actions=actions,
                observations=observations,
                ui_findings=ui_findings,
                visual=False,
                click_pulse_enabled=False,
                visual_human_mouse=False,
                visual_mouse_speed=1.0,
                visual_click_hold_ms=120,
                timeout_ms=1000,
                movement_capture_dir=None,
                evidence_paths=[],
                disable_active_youtube_iframe_pointer_events=lambda _p: None,
                force_main_frame_context=lambda _p: True,
                restore_iframe_pointer_events=lambda _p, _g: None,
                retry_scroll=lambda *_a, **_kw: None,
                scan_visible_buttons_in_cards=lambda *_a, **_kw: ([], True),
                scan_visible_selectors=lambda **_kw: [],
                safe_page_title=lambda _p: "Audio3",
                is_timeout_error=lambda _e: False,
                to_repo_rel=lambda p: str(p),
            )

        self.assertIn("no matching clickable targets", str(ctx.exception))
        self.assertTrue(any("clicked=0" in item for item in ui_findings))

    def test_target_not_found_handoff_classifies_no_effect_click(self) -> None:
        ui_findings: list[str] = []
        updates = target_not_found_handoff(
            teaching_mode=True,
            step_kind="click_selector",
            step_target="[id^=track-play-]",
            interactive_step=6,
            learning_notes=[],
            ui_findings=ui_findings,
            page=None,
            show_teaching_notice=lambda _page, _target: None,
            failure_message=(
                "Bulk click in cards found no matching clickable targets: "
                "selector=[id^=track-play-] cards=.track-card text=Virginia Beach"
            ),
        )
        self.assertTrue(updates.get("should_break"))
        self.assertIn("what_failed=no_effect_click", ui_findings)
        self.assertTrue(any("card scan" in item for item in ui_findings))


if __name__ == "__main__":
    unittest.main()
