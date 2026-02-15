import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.web_session import (
    WebSession,
    create_session,
    get_last_session,
    refresh_session_state,
    save_session,
)


class WebSessionTests(unittest.TestCase):
    def test_session_state_sync_when_pid_dies(self) -> None:
        session = WebSession(
            session_id="s1",
            pid=99999,
            port=9222,
            user_data_dir="/tmp/x",
            browser_binary="/usr/bin/chromium",
            url="http://localhost:5173",
            title="Audio3",
            controlled=True,
            created_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-01-01T00:00:00+00:00",
            state="open",
        )
        with patch("bridge.web_session._pid_alive", return_value=False), patch(
            "bridge.web_session._cdp_alive", return_value=False
        ), patch("bridge.web_session.save_session") as save_mock:
            refreshed = refresh_session_state(session)
        self.assertEqual(refreshed.state, "closed")
        self.assertFalse(refreshed.controlled)
        save_mock.assert_called()

    def test_status_refresh_persists_closed_state(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            base = Path(tmp)
            sessions_dir = base / "web_sessions"
            sessions_dir.mkdir(parents=True)
            index_path = sessions_dir / "index.json"
            session = WebSession(
                session_id="s2",
                pid=10001,
                port=9333,
                user_data_dir="/tmp/y",
                browser_binary="/usr/bin/chromium",
                url="http://localhost:5173",
                title="Audio3",
                controlled=False,
                created_at="2026-01-01T00:00:00+00:00",
                last_seen_at="2026-01-01T00:00:00+00:00",
                state="open",
            )
            with patch("bridge.web_session.SESSIONS_DIR", sessions_dir), patch(
                "bridge.web_session.INDEX_PATH", index_path
            ):
                save_session(session)
                index_path.write_text(json.dumps({"last_session_id": "s2"}), encoding="utf-8")
                with patch("bridge.web_session._pid_alive", return_value=False), patch(
                    "bridge.web_session._cdp_alive", return_value=False
                ):
                    refreshed = get_last_session()
                self.assertIsNotNone(refreshed)
                assert refreshed is not None
                self.assertEqual(refreshed.state, "closed")
                payload = json.loads((sessions_dir / "s2.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["state"], "closed")

    def test_web_open_process_detached_survives_cli_return(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            base = Path(tmp)
            sessions_dir = base / "web_sessions"
            sessions_dir.mkdir(parents=True)
            index_path = sessions_dir / "index.json"

            captured = {}

            class _Proc:
                pid = 4242

            def fake_popen(cmd, **kwargs):
                captured["cmd"] = cmd
                captured["kwargs"] = kwargs
                return _Proc()

            with patch("bridge.web_session.SESSIONS_DIR", sessions_dir), patch(
                "bridge.web_session.INDEX_PATH", index_path
            ), patch("bridge.web_session._find_browser_binary", return_value="/usr/bin/chromium"), patch(
                "bridge.web_session._get_free_port", side_effect=[9444, 9555]
            ), patch("bridge.web_session._wait_for_cdp"), patch(
                "bridge.web_session._wait_for_agent"
            ), patch(
                "bridge.web_session.subprocess.Popen", side_effect=fake_popen
            ):
                session = create_session("http://127.0.0.1:5180")

            self.assertEqual(session.pid, 4242)
            self.assertTrue(captured["kwargs"]["start_new_session"])
            self.assertTrue(captured["kwargs"]["close_fds"])
            self.assertEqual(captured["kwargs"]["stdin"], __import__("subprocess").DEVNULL)


if __name__ == "__main__":
    unittest.main()
