import unittest
from unittest.mock import patch

from bridge.web_backend import _highlight_target, _preflight_target_reachable


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


if __name__ == "__main__":
    unittest.main()
