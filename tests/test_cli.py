import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bridge.constants import SHELL_ALLOWED_COMMAND_PREFIXES, WEB_ALLOWED_COMMAND_PREFIXES
from bridge.cli import (
    _validate_evidence_paths,
    _validate_report_actions,
    main,
    logs_command,
    run_command,
    web_close_command,
    web_open_command,
    web_release_command,
)
from bridge.models import OIReport
from bridge.web_session import WebSession


class CLITests(unittest.TestCase):
    def test_actions_without_cmd_prefix_are_blocked(self) -> None:
        report = OIReport(
            task_id="t1",
            goal="inspect",
            actions=["attempt to check ui"],
            observations=[],
            console_errors=[],
            network_findings=[],
            ui_findings=[],
            result="failed",
            evidence_paths=[],
        )
        with self.assertRaises(SystemExit):
            _validate_report_actions(
                report,
                confirm_sensitive=True,
                expected_targets=set(),
                allowlist=SHELL_ALLOWED_COMMAND_PREFIXES,
                mode="shell",
            )

    def test_evidence_path_outside_run_dir_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            run_dir = base / "runs" / "r1"
            run_dir.mkdir(parents=True)
            outside = base / "outside.txt"
            outside.write_text("x", encoding="utf-8")
            report = OIReport(
                task_id="t1",
                goal="inspect",
                actions=["cmd: ls -la"],
                observations=[],
                console_errors=[],
                network_findings=[],
                ui_findings=[],
                result="failed",
                evidence_paths=[str(outside)],
            )
            with self.assertRaises(SystemExit):
                _validate_evidence_paths(
                    report,
                    run_dir,
                    mode="shell",
                    click_steps=0,
                    run_id="r1",
                )

    def test_logs_include_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            (run_dir / "bridge.log").write_text("bridge-line\n", encoding="utf-8")
            (run_dir / "oi_stdout.log").write_text("stdout-line\n", encoding="utf-8")
            (run_dir / "oi_stderr.log").write_text("stderr-line\n", encoding="utf-8")
            payload = {"run_dir": str(run_dir)}
            with patch("bridge.cli.status_payload", return_value=payload):
                out = io.StringIO()
                with redirect_stdout(out):
                    logs_command(200)
            text = out.getvalue()
            self.assertIn("bridge-line", text)
            self.assertIn("stdout-line", text)
            self.assertIn("stderr-line", text)

    def test_url_target_drift_is_blocked_for_network_actions(self) -> None:
        report = OIReport(
            task_id="t1",
            goal="api-check",
            actions=["cmd: curl -s http://localhost/health"],
            observations=[],
            console_errors=[],
            network_findings=[],
            ui_findings=[],
            result="failed",
            evidence_paths=[],
        )
        with self.assertRaises(SystemExit):
            _validate_report_actions(
                report,
                confirm_sensitive=True,
                expected_targets={"http://127.0.0.1:8000"},
                allowlist=SHELL_ALLOWED_COMMAND_PREFIXES,
                mode="shell",
            )

    def test_url_target_exact_match_is_allowed(self) -> None:
        report = OIReport(
            task_id="t1",
            goal="api-check",
            actions=["cmd: curl -s http://127.0.0.1:8000/health"],
            observations=[],
            console_errors=[],
            network_findings=[],
            ui_findings=[],
            result="success",
            evidence_paths=[],
        )
        _validate_report_actions(
            report,
            confirm_sensitive=True,
            expected_targets={"http://127.0.0.1:8000"},
            allowlist=SHELL_ALLOWED_COMMAND_PREFIXES,
            mode="shell",
        )

    def test_malformed_command_missing_executable_prefix_is_blocked(self) -> None:
        report = OIReport(
            task_id="t2",
            goal="api",
            actions=["cmd: -H 'Authorization: Bearer token'"],
            observations=[],
            console_errors=[],
            network_findings=[],
            ui_findings=[],
            result="failed",
            evidence_paths=[],
        )
        with self.assertRaises(SystemExit):
            _validate_report_actions(
                report,
                confirm_sensitive=True,
                expected_targets=set(),
                allowlist=SHELL_ALLOWED_COMMAND_PREFIXES,
                mode="shell",
            )

    def test_malformed_multiline_command_is_blocked(self) -> None:
        report = OIReport(
            task_id="t3",
            goal="api",
            actions=["cmd: curl https://api.example.com\n-H 'Authorization: Bearer token'"],
            observations=[],
            console_errors=[],
            network_findings=[],
            ui_findings=[],
            result="failed",
            evidence_paths=[],
        )
        with self.assertRaises(SystemExit):
            _validate_report_actions(
                report,
                confirm_sensitive=True,
                expected_targets=set(),
                allowlist=SHELL_ALLOWED_COMMAND_PREFIXES,
                mode="shell",
            )

    def test_run_command_writes_failed_report_on_runtime_exception(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            ctx = type(
                "RunContext",
                (),
                {
                    "run_id": "r1",
                    "run_dir": run_dir,
                    "bridge_log": run_dir / "bridge.log",
                    "stdout_log": run_dir / "oi_stdout.log",
                    "stderr_log": run_dir / "oi_stderr.log",
                    "report_path": run_dir / "report.json",
                },
            )()
            status_path = Path(tmp) / "status.json"

            with patch("bridge.cli.create_run_context", return_value=ctx), patch(
                "bridge.cli._preflight_runtime"
            ), patch("bridge.cli.require_sensitive_confirmation"), patch(
                "bridge.cli.write_status",
                side_effect=lambda **kwargs: Path(status_path).write_text(
                    json.dumps(kwargs, default=str), encoding="utf-8"
                ),
            ), patch(
                "bridge.cli.run_web_task",
                side_effect=SystemExit("web backend boom"),
            ):
                with self.assertRaises(SystemExit):
                    run_command("abre http://localhost:5173", confirm_sensitive=True, mode="web")

            report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["result"], "failed")
            self.assertTrue(report["console_errors"])
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["result"], "failed")
            self.assertEqual(status["state"], "completed")

    def test_run_command_web_updates_running_progress_status(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r2"
            run_dir.mkdir(parents=True)
            evidence = run_dir / "evidence"
            evidence.mkdir(parents=True)
            (evidence / "step_1_before.png").write_bytes(b"png")
            (evidence / "step_1_after.png").write_bytes(b"png")
            ctx = type(
                "RunContext",
                (),
                {
                    "run_id": "r2",
                    "run_dir": run_dir,
                    "bridge_log": run_dir / "bridge.log",
                    "stdout_log": run_dir / "oi_stdout.log",
                    "stderr_log": run_dir / "oi_stderr.log",
                    "report_path": run_dir / "report.json",
                },
            )()
            status_path = Path(tmp) / "status.json"
            snapshots: list[dict] = []

            def fake_write_status(**kwargs):
                snapshots.append(dict(kwargs))
                status_path.write_text(json.dumps(kwargs, default=str), encoding="utf-8")

            def fake_run_web_task(
                task,
                run_dir,
                timeout_seconds,
                verified,
                progress_cb,
                visual=False,
                visual_cursor=True,
                visual_click_pulse=True,
                visual_scale=1.0,
                visual_color="#3BA7FF",
                visual_human_mouse=True,
                visual_mouse_speed=1.0,
                visual_click_hold_ms=180,
                session=None,
                keep_open=False,
            ):
                progress_cb(1, 1, "web step 1/1: click_text")
                return OIReport(
                    task_id="r2",
                    goal="web: http://localhost:5173",
                    actions=[
                        "cmd: playwright goto http://localhost:5173",
                        "cmd: playwright click text:Entrar demo",
                    ],
                    observations=["Opened URL", "Clicked text in step 1"],
                    console_errors=[],
                    network_findings=[],
                    ui_findings=["step 1 verify visible result"],
                    result="success",
                    evidence_paths=[
                        str((evidence / "step_1_before.png").resolve().relative_to(Path.cwd())),
                        str((evidence / "step_1_after.png").resolve().relative_to(Path.cwd())),
                    ],
                )

            with patch("bridge.cli.create_run_context", return_value=ctx), patch(
                "bridge.cli._preflight_runtime"
            ), patch("bridge.cli.require_sensitive_confirmation"), patch(
                "bridge.cli.write_status",
                side_effect=fake_write_status,
            ), patch(
                "bridge.cli.run_web_task",
                side_effect=fake_run_web_task,
            ):
                with redirect_stdout(io.StringIO()):
                    run_command(
                        "abre http://localhost:5173 y haz click en 'Entrar demo'",
                        confirm_sensitive=True,
                        mode="web",
                    )

            self.assertTrue(any(item.get("state") == "running" for item in snapshots))
            self.assertTrue(
                any(item.get("progress") == "web step 1/1: click_text" for item in snapshots)
            )
            final_status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(final_status["state"], "completed")
            self.assertEqual(final_status["result"], "success")

    def test_run_command_timeout_error_closes_failed_report_and_status(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r3"
            run_dir.mkdir(parents=True)
            ctx = type(
                "RunContext",
                (),
                {
                    "run_id": "r3",
                    "run_dir": run_dir,
                    "bridge_log": run_dir / "bridge.log",
                    "stdout_log": run_dir / "oi_stdout.log",
                    "stderr_log": run_dir / "oi_stderr.log",
                    "report_path": run_dir / "report.json",
                },
            )()
            status_path = Path(tmp) / "status.json"

            with patch("bridge.cli.create_run_context", return_value=ctx), patch(
                "bridge.cli._preflight_runtime"
            ), patch("bridge.cli.require_sensitive_confirmation"), patch(
                "bridge.cli.write_status",
                side_effect=lambda **kwargs: Path(status_path).write_text(
                    json.dumps(kwargs, default=str), encoding="utf-8"
                ),
            ), patch(
                "bridge.cli.run_web_task",
                side_effect=TimeoutError("web step timeout"),
            ):
                with self.assertRaises(SystemExit):
                    run_command("abre http://localhost:5173", confirm_sensitive=True, mode="web")

            report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["result"], "failed")
            self.assertTrue(any("timeout" in item.lower() for item in report["console_errors"]))
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["result"], "failed")
            self.assertEqual(status["state"], "completed")

    def test_visual_flag_only_supported_in_web_mode(self) -> None:
        with self.assertRaises(SystemExit):
            run_command("window:list", confirm_sensitive=True, mode="gui", visual=True)

    def test_visual_mouse_speed_must_be_positive(self) -> None:
        with self.assertRaises(SystemExit):
            run_command(
                "abre http://localhost:5173",
                confirm_sensitive=True,
                mode="web",
                visual_mouse_speed=0,
            )

    def test_visual_click_hold_must_be_non_negative(self) -> None:
        with self.assertRaises(SystemExit):
            run_command(
                "abre http://localhost:5173",
                confirm_sensitive=True,
                mode="web",
                visual_click_hold_ms=-1,
            )

    def test_web_wait_steps_do_not_increment_interactive_evidence_count(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r4"
            evidence = run_dir / "evidence"
            evidence.mkdir(parents=True)
            before = evidence / "step_1_before.png"
            after = evidence / "step_1_after.png"
            before.write_bytes(b"png")
            after.write_bytes(b"png")
            report = OIReport(
                task_id="r4",
                goal="web",
                actions=[
                    "cmd: playwright goto http://localhost:5173",
                    "cmd: playwright wait selector:#form",
                    "cmd: playwright wait text:Cargando",
                    "cmd: playwright click text:Entrar demo",
                ],
                observations=["Opened URL", "Waited selector", "Clicked text in step 1"],
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
            self.assertEqual(click_steps, 1)
            safe = _validate_evidence_paths(
                report,
                run_dir,
                mode="web",
                click_steps=click_steps,
                run_id="r4",
            )
            self.assertEqual(len(safe), 2)

    def test_web_open_release_close_lifecycle(self) -> None:
        session = WebSession(
            session_id="s1",
            pid=101,
            port=9222,
            user_data_dir="/tmp/x",
            browser_binary="/usr/bin/chromium",
            url="http://localhost:5173",
            title="Audio3",
            controlled=False,
            created_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-01-01T00:00:00+00:00",
            state="open",
        )
        out = io.StringIO()
        with patch("bridge.cli.get_last_session", return_value=None), patch(
            "bridge.cli.create_session", return_value=session
        ):
            with redirect_stdout(out):
                web_open_command("http://localhost:5173")
        self.assertIn('"session_id": "s1"', out.getvalue())

        out = io.StringIO()
        with patch("bridge.cli.load_and_refresh_session", return_value=session), patch(
            "bridge.cli.session_is_alive", return_value=True
        ), patch("bridge.cli.release_session_control_overlay"), patch(
            "bridge.cli.mark_controlled"
        ):
            with redirect_stdout(out):
                web_release_command("s1")
        self.assertIn('"result": "released"', out.getvalue())

        out = io.StringIO()
        with patch("bridge.cli.load_and_refresh_session", return_value=session), patch(
            "bridge.cli.session_is_alive", return_value=True
        ), patch("bridge.cli.release_session_control_overlay"), patch(
            "bridge.cli.close_session"
        ):
            with redirect_stdout(out):
                web_close_command("s1")
        self.assertIn('"state": "closed"', out.getvalue())

    def test_keep_open_does_not_close_persistent_browser(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "rk"
            run_dir.mkdir(parents=True)
            evidence = run_dir / "evidence"
            evidence.mkdir(parents=True)
            (evidence / "step_1_before.png").write_bytes(b"png")
            (evidence / "step_1_after.png").write_bytes(b"png")
            ctx = type(
                "RunContext",
                (),
                {
                    "run_id": "rk",
                    "run_dir": run_dir,
                    "bridge_log": run_dir / "bridge.log",
                    "stdout_log": run_dir / "oi_stdout.log",
                    "stderr_log": run_dir / "oi_stderr.log",
                    "report_path": run_dir / "report.json",
                },
            )()
            session = WebSession(
                session_id="sk",
                pid=101,
                port=9222,
                user_data_dir="/tmp/x",
                browser_binary="/usr/bin/chromium",
                url="about:blank",
                title="",
                controlled=False,
                created_at="2026-01-01T00:00:00+00:00",
                last_seen_at="2026-01-01T00:00:00+00:00",
                state="open",
            )

            def fake_run_web_task(*args, **kwargs):
                self.assertEqual(kwargs["session"].session_id, "sk")
                self.assertTrue(kwargs["keep_open"])
                return OIReport(
                    task_id="rk",
                    goal="web: http://localhost:5173",
                    actions=[
                        "cmd: playwright goto http://localhost:5173",
                        "cmd: playwright click text:Entrar demo",
                    ],
                    observations=["Opened URL", "Clicked text in step 1"],
                    console_errors=[],
                    network_findings=[],
                    ui_findings=["step 1 verify visible result", "control released"],
                    result="success",
                    evidence_paths=[
                        str((evidence / "step_1_before.png").resolve().relative_to(Path.cwd())),
                        str((evidence / "step_1_after.png").resolve().relative_to(Path.cwd())),
                    ],
                )

            with patch("bridge.cli.create_run_context", return_value=ctx), patch(
                "bridge.cli._preflight_runtime"
            ), patch("bridge.cli.require_sensitive_confirmation"), patch(
                "bridge.cli.write_status"
            ), patch("bridge.cli.create_session", return_value=session), patch(
                "bridge.cli.run_web_task",
                side_effect=fake_run_web_task,
            ), patch("bridge.cli.mark_controlled") as mark_mock:
                with redirect_stdout(io.StringIO()):
                    run_command(
                        "abre http://localhost:5173 y haz click en 'Entrar demo'",
                        confirm_sensitive=True,
                        mode="web",
                        keep_open=True,
                    )
            mark_mock.assert_called()

    def test_status_includes_web_session(self) -> None:
        session = WebSession(
            session_id="s9",
            pid=109,
            port=9333,
            user_data_dir="/tmp/x",
            browser_binary="/usr/bin/chromium",
            url="http://localhost:5173",
            title="Audio3",
            controlled=True,
            created_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-01-01T00:00:00+00:00",
            state="open",
        )
        out = io.StringIO()
        with patch("bridge.cli.status_payload", return_value={"status": "ok"}), patch(
            "bridge.cli.get_last_session", return_value=session
        ), patch("bridge.cli.refresh_session_state", return_value=session), patch(
            "sys.argv", ["bridge", "status"]
        ):
            with redirect_stdout(out):
                main()
        self.assertIn('"web_session"', out.getvalue())
        self.assertIn('"controlled": true', out.getvalue())

    def test_attach_refreshes_liveness_before_use(self) -> None:
        dead = WebSession(
            session_id="dead1",
            pid=111,
            port=9333,
            user_data_dir="/tmp/x",
            browser_binary="/usr/bin/chromium",
            url="http://localhost:5173",
            title="Audio3",
            controlled=False,
            created_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-01-01T00:00:00+00:00",
            state="closed",
        )
        with patch("bridge.cli.load_and_refresh_session", return_value=dead), patch(
            "bridge.cli.session_is_alive", return_value=False
        ), patch("bridge.cli._preflight_runtime"), patch(
            "bridge.cli.require_sensitive_confirmation"
        ), self.assertRaises(SystemExit) as ctx:
            run_command(
                "abre http://localhost:5173",
                confirm_sensitive=True,
                mode="web",
                attach_session_id="dead1",
            )
        self.assertIn("run web-open again", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
