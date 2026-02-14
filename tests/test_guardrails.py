import unittest

from bridge.guardrails import (
    evaluate_command,
    require_sensitive_confirmation,
    task_has_sensitive_intent,
    task_violates_code_edit_rule,
)


class GuardrailTests(unittest.TestCase):
    def test_task_blocks_code_edit_intent(self) -> None:
        self.assertTrue(task_violates_code_edit_rule("please edit app.py to fix bug"))
        self.assertFalse(task_violates_code_edit_rule("open the browser and inspect ui"))

    def test_allowlisted_command_is_allowed(self) -> None:
        decision = evaluate_command("ls -la")
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.sensitive)

    def test_blocked_command_is_rejected(self) -> None:
        decision = evaluate_command("rm -rf /tmp/demo")
        self.assertFalse(decision.allowed)

    def test_non_allowlisted_command_is_rejected(self) -> None:
        decision = evaluate_command("python script.py")
        self.assertFalse(decision.allowed)

    def test_redirection_token_remains_blocked(self) -> None:
        decision = evaluate_command("echo hi > output.txt")
        self.assertFalse(decision.allowed)

    def test_tee_token_remains_blocked(self) -> None:
        decision = evaluate_command("ls | tee output.txt")
        self.assertFalse(decision.allowed)

    def test_sensitive_command_requires_confirmation(self) -> None:
        with self.assertRaises(PermissionError):
            require_sensitive_confirmation(
                ["curl https://example.com"],
                auto_confirm=False,
            )

    def test_task_sensitive_intent_detection(self) -> None:
        hits = task_has_sensitive_intent("usa curl para consultar endpoint")
        self.assertEqual(hits, ["curl"])


if __name__ == "__main__":
    unittest.main()
