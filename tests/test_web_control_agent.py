import os
import unittest
from unittest.mock import patch

from bridge.web_control_agent import _AgentRuntime


class WebControlAgentTests(unittest.TestCase):
    def test_minimal_user_control_drops_trivial_events(self) -> None:
        runtime = _AgentRuntime()
        with patch.dict(os.environ, {"BRIDGE_OBSERVER_NOISE_MODE": "minimal"}, clear=False):
            runtime.record_event({"type": "mousemove", "controlled": False, "learning_active": False})
            runtime.record_event({"type": "scroll", "controlled": False, "learning_active": False})
            runtime.record_event({"type": "click", "controlled": False, "learning_active": False})
            snapshot = runtime.snapshot()
        self.assertEqual(snapshot["recent_events"], [])

    def test_debug_mode_keeps_user_control_events(self) -> None:
        runtime = _AgentRuntime()
        with patch.dict(os.environ, {"BRIDGE_OBSERVER_NOISE_MODE": "debug"}, clear=False):
            runtime.record_event({"type": "mousemove", "controlled": False, "learning_active": False})
            runtime.record_event({"type": "scroll", "controlled": False, "learning_active": False})
            runtime.record_event({"type": "click", "controlled": False, "learning_active": False})
            snapshot = runtime.snapshot()
        event_types = [evt.get("type") for evt in snapshot["recent_events"]]
        self.assertIn("mousemove", event_types)
        self.assertIn("scroll", event_types)
        self.assertIn("click", event_types)

    def test_learning_window_keeps_click_even_if_event_flag_missing(self) -> None:
        runtime = _AgentRuntime()
        with patch.dict(os.environ, {"BRIDGE_OBSERVER_NOISE_MODE": "minimal"}, clear=False):
            runtime.record_event({"type": "learning_on", "window_seconds": 25})
            runtime.record_event({"type": "click", "controlled": False, "learning_active": False})
            snapshot = runtime.snapshot()
        event_types = [evt.get("type") for evt in snapshot["recent_events"]]
        self.assertIn("click", event_types)


if __name__ == "__main__":
    unittest.main()
