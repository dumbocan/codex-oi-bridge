"""Guardrails to keep Open Interpreter in observation-only mode."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass

from bridge.constants import (
    BLOCKED_COMMAND_TOKENS,
    CODE_EXTENSIONS,
    SHELL_ALLOWED_COMMAND_PREFIXES,
    SENSITIVE_COMMAND_TOKENS,
)


@dataclass(frozen=True)
class GuardrailDecision:
    allowed: bool
    reason: str
    sensitive: bool = False


def task_violates_code_edit_rule(task: str) -> bool:
    normalized = task.lower()
    edit_words = ("edit", "modify", "write", "refactor", "patch", "implement", "create file")
    if any(word in normalized for word in edit_words):
        if any(ext in normalized for ext in CODE_EXTENSIONS):
            return True
    return False


def task_has_sensitive_intent(task: str) -> list[str]:
    normalized = task.lower()
    hits: list[str] = []
    for token in SENSITIVE_COMMAND_TOKENS:
        if token in normalized:
            hits.append(token)
    return hits


def evaluate_command(
    command: str,
    *,
    allowlist: tuple[str, ...] = SHELL_ALLOWED_COMMAND_PREFIXES,
) -> GuardrailDecision:
    try:
        parts = shlex.split(command)
    except ValueError:
        return GuardrailDecision(False, "Malformed shell command")

    if not parts:
        return GuardrailDecision(False, "Empty command")

    token_set = set(parts)
    for blocked in BLOCKED_COMMAND_TOKENS:
        if blocked in token_set or re.search(rf"\b{re.escape(blocked)}\b", command):
            return GuardrailDecision(False, f"Blocked command token detected: {blocked}")

    prefix = parts[0]
    if prefix not in allowlist:
        return GuardrailDecision(False, f"Command not in allowlist: {prefix}")

    sensitive = any(
        token in token_set or re.search(rf"\b{re.escape(token)}\b", command)
        for token in SENSITIVE_COMMAND_TOKENS
    )
    if sensitive:
        return GuardrailDecision(True, "Sensitive command requires explicit confirmation", True)
    return GuardrailDecision(True, "Allowed command", False)


def require_sensitive_confirmation(sensitive_items: list[str], auto_confirm: bool) -> None:
    if not sensitive_items:
        return
    if auto_confirm:
        return
    if not os.isatty(0):
        raise PermissionError(
            "Sensitive actions detected but no TTY for confirmation. "
            "Use --confirm-sensitive to proceed."
        )

    print("Sensitive actions detected:")
    for item in sensitive_items:
        print(f"- {item}")
    answer = input("Type YES to continue: ").strip()
    if answer != "YES":
        raise PermissionError("Sensitive actions rejected by user")
