import unittest

from bridge.parser import parse_oi_report


class ParserTests(unittest.TestCase):
    def test_parse_valid_strict_json_report(self) -> None:
        raw = """
noise before
{
  "task_id": "t-1",
  "goal": "Inspect UI",
  "actions": ["cmd: ls -la", "checked console"],
  "observations": ["header visible"],
  "console_errors": [],
  "network_findings": ["GET /api 200"],
  "ui_findings": ["button disabled"],
  "result": "partial",
  "evidence_paths": ["runs/20260101-120000/screenshot.png"]
}
"""
        report = parse_oi_report(raw)
        self.assertEqual(report.task_id, "t-1")
        self.assertEqual(report.result, "partial")

    def test_parse_rejects_extra_keys(self) -> None:
        raw = """
{
  "task_id": "t-1",
  "goal": "Inspect UI",
  "actions": [],
  "observations": [],
  "console_errors": [],
  "network_findings": [],
  "ui_findings": [],
  "result": "success",
  "evidence_paths": [],
  "extra": "not allowed"
}
"""
        with self.assertRaises(ValueError):
            parse_oi_report(raw)

    def test_parse_coerces_invalid_result(self) -> None:
        raw = """
{
  "task_id": "t-1",
  "goal": "Inspect UI",
  "actions": [],
  "observations": [],
  "console_errors": [],
  "network_findings": [],
  "ui_findings": [],
  "result": "ok",
  "evidence_paths": []
}
"""
        report = parse_oi_report(raw)
        self.assertEqual(report.result, "success")

    def test_parse_skips_invalid_json_reports_and_uses_valid_one(self) -> None:
        raw = """
some text
{
  "task_id": "x-1",
  "goal": "bad",
  "actions": [],
  "observations": [],
  "console_errors": [],
  "network_findings": [],
  "ui_findings": [],
  "result": "done",
  "evidence_paths": []
}
more text
{
  "task_id": "x-2",
  "goal": "good",
  "actions": [],
  "observations": [],
  "console_errors": [],
  "network_findings": [],
  "ui_findings": [],
  "result": "success",
  "evidence_paths": []
}
"""
        report = parse_oi_report(raw)
        self.assertEqual(report.task_id, "x-2")
        self.assertEqual(report.result, "success")

    def test_parse_coerces_action_objects_and_result_text(self) -> None:
        raw = """
{
  "task_id": "t-2",
  "goal": "inspect",
  "actions": [
    {"action": "Attempt screenshot", "details": "Saved in XWD format"}
  ],
  "observations": [],
  "console_errors": [],
  "network_findings": [],
  "ui_findings": [],
  "result": "Screenshot saved but conversion failed",
  "evidence_paths": ["runs/x/screenshot.xwd"]
}
"""
        report = parse_oi_report(raw)
        self.assertEqual(
            report.actions,
            ["Attempt screenshot: Saved in XWD format"],
        )
        self.assertEqual(report.result, "failed")


if __name__ == "__main__":
    unittest.main()
