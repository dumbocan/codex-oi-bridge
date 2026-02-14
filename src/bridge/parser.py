"""Parser helpers for extracting strict JSON output from Open Interpreter."""

from __future__ import annotations

import json
from typing import Any

from bridge.models import OIReport


def extract_first_json_object(text: str) -> dict:
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError("No valid JSON object found in Open Interpreter output")


def parse_oi_report(raw_output: str) -> OIReport:
    decoder = json.JSONDecoder()
    best_payload: dict[str, Any] | None = None
    best_score = -1
    best_report: OIReport | None = None
    best_report_score = -1
    last_error: Exception | None = None

    for idx, char in enumerate(raw_output):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(raw_output[idx:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        score = _candidate_score(payload)
        if score > best_score:
            best_payload = payload
            best_score = score

        try:
            report = OIReport.from_dict(_coerce_payload(payload))
            if score >= best_report_score:
                best_report = report
                best_report_score = score
        except ValueError as exc:
            last_error = exc
            continue

    if best_report is not None:
        return best_report

    if best_payload is not None:
        try:
            return OIReport.from_dict(_coerce_payload(best_payload))
        except ValueError as exc:
            last_error = exc
    if best_payload is not None and last_error is not None:
        raise ValueError(f"JSON found but report is invalid: {last_error}")
    raise ValueError("No valid JSON object found in Open Interpreter output")


def _candidate_score(payload: dict[str, Any]) -> int:
    # Prefer dicts that look like our report schema.
    expected = {
        "task_id",
        "goal",
        "actions",
        "observations",
        "console_errors",
        "network_findings",
        "ui_findings",
        "result",
        "evidence_paths",
    }
    keys = set(payload.keys())
    return len(keys & expected)


def _coerce_payload(payload: dict[str, Any]) -> dict[str, Any]:
    coerced = dict(payload)
    if "actions" in coerced:
        coerced["actions"] = _coerce_string_list(coerced["actions"], key_hint="action")
    for key in (
        "observations",
        "console_errors",
        "network_findings",
        "ui_findings",
        "evidence_paths",
    ):
        if key in coerced:
            coerced[key] = _coerce_string_list(coerced[key], key_hint=key)
    if "result" in coerced:
        coerced["result"] = _coerce_result(coerced["result"])
    return coerced


def _coerce_string_list(value: Any, key_hint: str) -> list[str]:
    if not isinstance(value, list):
        return [str(value)]
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
            continue
        if isinstance(item, dict):
            action = str(item.get("action", "")).strip()
            details = str(item.get("details", "")).strip()
            if action and details:
                out.append(f"{action}: {details}")
            elif action:
                out.append(action)
            elif details:
                out.append(details)
            else:
                out.append(json.dumps(item, ensure_ascii=False))
            continue
        out.append(str(item))
    return out


def _coerce_result(value: Any) -> str:
    text = str(value).strip().lower()
    if text in {"success", "partial", "failed"}:
        return text
    if any(token in text for token in ("fail", "error", "denied", "blocked")):
        return "failed"
    if any(token in text for token in ("partial", "unable", "missing", "not ", "can't")):
        return "partial"
    if any(token in text for token in ("success", "completed", "done", "ok")):
        return "success"
    return "partial"
