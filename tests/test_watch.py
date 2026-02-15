import io
import unittest
from contextlib import redirect_stdout

from bridge.watch import _watch_loop


class WatchTests(unittest.TestCase):
    def test_prints_only_new_events(self) -> None:
        states = [
            {
                "incident_open": False,
                "ack_count": 0,
                "last_event_at": "2026-02-15T10:00:00+00:00",
                "recent_events": [
                    {
                        "type": "click",
                        "severity": "info",
                        "message": "click Play",
                        "target": "Play",
                        "url": "/",
                        "status": 0,
                        "created_at": "2026-02-15T10:00:00+00:00",
                    }
                ],
            },
            {
                "incident_open": False,
                "ack_count": 0,
                "last_event_at": "2026-02-15T10:00:01+00:00",
                "recent_events": [
                    {
                        "type": "click",
                        "severity": "info",
                        "message": "click Play",
                        "target": "Play",
                        "url": "/",
                        "status": 0,
                        "created_at": "2026-02-15T10:00:00+00:00",
                    },
                    {
                        "type": "network_error",
                        "severity": "error",
                        "message": "http 502",
                        "target": "",
                        "url": "/tracks/1/stream",
                        "status": 502,
                        "created_at": "2026-02-15T10:00:01+00:00",
                    },
                ],
            },
        ]
        idx = {"i": 0}

        def fetch_state():
            i = idx["i"]
            idx["i"] = min(i + 1, len(states) - 1)
            return states[i]

        sleep_calls = {"n": 0}

        def sleep_fn(_seconds: float) -> None:
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 2:
                raise KeyboardInterrupt

        out = io.StringIO()
        with redirect_stdout(out):
            _watch_loop(
                fetch_state=fetch_state,
                sleep_fn=sleep_fn,
                interval_ms=50,
                since_last=True,
                json_mode=False,
                print_events=0,
                only="info",
                notify=False,
            )

        text = out.getvalue()
        self.assertIn("ERROR http 502", text)
        self.assertNotIn('target="Play"', text)

    def test_detects_incident_transition(self) -> None:
        states = [
            {
                "incident_open": False,
                "ack_count": 0,
                "last_error": "",
                "recent_events": [],
                "last_event_at": "",
            },
            {
                "incident_open": True,
                "ack_count": 0,
                "last_error": "http 502",
                "error_count": 1,
                "recent_events": [],
                "last_event_at": "",
            },
        ]
        idx = {"i": 0}

        def fetch_state():
            i = idx["i"]
            idx["i"] = min(i + 1, len(states) - 1)
            return states[i]

        def sleep_fn(_seconds: float) -> None:
            sleep_fn.calls += 1
            if sleep_fn.calls >= 2:
                raise KeyboardInterrupt

        sleep_fn.calls = 0

        out = io.StringIO()
        with redirect_stdout(out):
            _watch_loop(
                fetch_state=fetch_state,
                sleep_fn=sleep_fn,
                interval_ms=50,
                since_last=False,
                json_mode=False,
                print_events=0,
                only="info",
                notify=False,
            )
        self.assertIn("INCIDENT OPEN: http 502", out.getvalue())

    def test_respects_only_errors_filter(self) -> None:
        states = [
            {
                "incident_open": False,
                "ack_count": 0,
                "last_event_at": "",
                "recent_events": [
                    {
                        "type": "click",
                        "severity": "info",
                        "message": "click X",
                        "target": "X",
                        "url": "/",
                        "status": 0,
                        "created_at": "2026-02-15T10:00:00+00:00",
                    },
                    {
                        "type": "network_warn",
                        "severity": "warn",
                        "message": "http 404",
                        "target": "",
                        "url": "/favicon.ico",
                        "status": 404,
                        "created_at": "2026-02-15T10:00:01+00:00",
                    },
                    {
                        "type": "network_error",
                        "severity": "error",
                        "message": "http 502",
                        "target": "",
                        "url": "/api",
                        "status": 502,
                        "created_at": "2026-02-15T10:00:02+00:00",
                    },
                ],
            }
        ]

        def fetch_state():
            return states[0]

        def sleep_fn(_seconds: float) -> None:
            raise KeyboardInterrupt

        out = io.StringIO()
        with redirect_stdout(out):
            _watch_loop(
                fetch_state=fetch_state,
                sleep_fn=sleep_fn,
                interval_ms=50,
                since_last=False,
                json_mode=False,
                print_events=3,
                only="errors",
                notify=False,
            )

        text = out.getvalue()
        self.assertIn("ERROR http 502", text)
        self.assertNotIn("click", text.lower())
        self.assertNotIn("WARN", text)


if __name__ == "__main__":
    unittest.main()
