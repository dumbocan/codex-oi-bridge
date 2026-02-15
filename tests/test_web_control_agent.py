import unittest
from unittest.mock import patch

from bridge.web_control_agent import perform_session_action
from bridge.web_session import WebSession


class WebControlAgentTests(unittest.TestCase):
    def _session(self) -> WebSession:
        return WebSession(
            session_id="s1",
            pid=101,
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
            agent_pid=202,
        )

    def test_release_action_works_without_active_run_process(self) -> None:
        session = self._session()
        released = self._session()
        released.controlled = False
        with patch("bridge.web_control_agent.load_session", return_value=session), patch(
            "bridge.web_control_agent.refresh_session_state", side_effect=[session, released]
        ), patch("bridge.web_control_agent.mark_controlled") as mark_mock, patch(
            "bridge.web_control_agent._update_overlay_state"
        ):
            payload, should_shutdown = perform_session_action("s1", "release")
        self.assertFalse(should_shutdown)
        mark_mock.assert_called_once()
        self.assertFalse(payload["controlled"])
        self.assertEqual(payload["message"], "control released")

    def test_close_action_works_without_active_run_process(self) -> None:
        session = self._session()
        closed = self._session()
        closed.state = "closed"
        closed.controlled = False
        with patch("bridge.web_control_agent.load_session", side_effect=[session, closed]), patch(
            "bridge.web_control_agent.refresh_session_state", side_effect=[session, closed]
        ), patch("bridge.web_control_agent.close_session") as close_mock, patch(
            "bridge.web_control_agent._update_overlay_state"
        ):
            payload, should_shutdown = perform_session_action("s1", "close")
        self.assertTrue(should_shutdown)
        close_mock.assert_called_once()
        self.assertEqual(payload["state"], "closed")
        self.assertFalse(payload["controlled"])

    def test_refresh_action_returns_updated_state(self) -> None:
        session = self._session()
        with patch("bridge.web_control_agent.load_session", return_value=session), patch(
            "bridge.web_control_agent.refresh_session_state", return_value=session
        ):
            payload, should_shutdown = perform_session_action("s1", "refresh")
        self.assertFalse(should_shutdown)
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(payload["state"], "open")

    def test_invalid_action_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            perform_session_action("s1", "noop")


if __name__ == "__main__":
    unittest.main()
