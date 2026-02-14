"""Data models and strict parsing for Open Interpreter reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from bridge.constants import ALLOWED_RESULT_VALUES, REQUIRED_REPORT_KEYS


@dataclass(frozen=True)
class OIReport:
    task_id: str
    goal: str
    actions: list[str]
    observations: list[str]
    console_errors: list[str]
    network_findings: list[str]
    ui_findings: list[str]
    result: str
    evidence_paths: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OIReport":
        keys = set(payload.keys())
        expected = set(REQUIRED_REPORT_KEYS)
        if keys != expected:
            missing = sorted(expected - keys)
            extra = sorted(keys - expected)
            raise ValueError(f"Invalid keys. missing={missing}, extra={extra}")

        report = cls(
            task_id=_expect_str(payload, "task_id"),
            goal=_expect_str(payload, "goal"),
            actions=_expect_str_list(payload, "actions"),
            observations=_expect_str_list(payload, "observations"),
            console_errors=_expect_str_list(payload, "console_errors"),
            network_findings=_expect_str_list(payload, "network_findings"),
            ui_findings=_expect_str_list(payload, "ui_findings"),
            result=_expect_str(payload, "result"),
            evidence_paths=_expect_str_list(payload, "evidence_paths"),
        )
        if report.result not in ALLOWED_RESULT_VALUES:
            raise ValueError(
                f"Invalid result '{report.result}'. Must be one of "
                f"{sorted(ALLOWED_RESULT_VALUES)}"
            )
        return report

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _expect_str(payload: dict[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string")
    return value


def _expect_str_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload[key]
    if not isinstance(value, list):
        raise ValueError(f"'{key}' must be a list of strings")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"'{key}' must contain only strings")
    return value
