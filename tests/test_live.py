import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bridge.live import live_command
from bridge.web_session import WebSession


class LiveCommandTests(unittest.TestCase):
    def _session(self) -> WebSession:
        return WebSession(
            session_id="s1",
            pid=123,
            port=9222,
            user_data_dir="/tmp/x",
            browser_binary="/usr/bin/chromium",
            url="http://localhost:5181",
            title="Audio3",
            controlled=False,
            created_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-01-01T00:00:00+00:00",
            state="open",
            control_port=9555,
            agent_pid=201,
        )

    def test_live_prints_only_on_change(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            (run_dir / "bridge.log").write_text("line-1\n", encoding="utf-8")
            (run_dir / "oi_stdout.log").write_text("", encoding="utf-8")
            (run_dir / "oi_stderr.log").write_text("", encoding="utf-8")

            session = self._session()
            payload = {
                "run_id": "r1",
                "run_dir": str(run_dir),
                "result": "running",
                "state": "running",
                "progress": "run started",
            }

            sleep_calls = {"n": 0}

            def fake_sleep(_sec: float) -> None:
                sleep_calls["n"] += 1
                if sleep_calls["n"] >= 2:
                    raise KeyboardInterrupt()

            out = io.StringIO()
            with patch("bridge.live.get_last_session", return_value=session), patch(
                "bridge.live.refresh_session_state", side_effect=lambda s: s
            ), patch("bridge.live.load_and_refresh_session", return_value=session), patch(
                "bridge.live.session_is_alive", return_value=True
            ), patch("bridge.live.session_agent_online", return_value=False), patch(
                "bridge.live.status_payload", return_value=payload
            ), patch("bridge.live.time.sleep", side_effect=fake_sleep):
                with redirect_stdout(out):
                    live_command(attach="last", interval_ms=100, tail=10, json_mode=False)

            text = out.getvalue()
            self.assertIn("run=r1", text)
            # quiet mode: same snapshot should not spam multiple blocks
            self.assertEqual(text.count("run=r1"), 1)

    def test_live_exits_cleanly_on_keyboard_interrupt_during_fetch(self) -> None:
        session = self._session()
        out = io.StringIO()
        with patch("bridge.live.get_last_session", return_value=session), patch(
            "bridge.live.refresh_session_state", return_value=session
        ), patch("bridge.live.session_is_alive", return_value=True), patch(
            "bridge.live.status_payload", side_effect=KeyboardInterrupt
        ):
            with redirect_stdout(out):
                live_command(attach="last", interval_ms=100, tail=10, json_mode=False)

        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
