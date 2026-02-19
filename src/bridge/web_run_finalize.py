"""Run finalization helpers for web executor outputs."""

from __future__ import annotations


def ensure_structured_ui_findings(
    ui_findings: list[str],
    *,
    result: str,
    where_default: str,
) -> None:
    keys = ("what_failed=", "where=", "why_likely=", "attempted=", "next_best_action=")
    has = {k: any(str(item).startswith(k) for item in ui_findings) for k in keys}
    if result == "success":
        defaults = {
            "what_failed=": "none",
            "where=": "n/a",
            "why_likely=": "n/a",
            "attempted=": "normal execution",
            "next_best_action=": "none",
        }
    else:
        defaults = {
            "what_failed=": "unknown",
            "where=": where_default or "web-run",
            "why_likely=": "run ended without explicit failure classification",
            "attempted=": "executor run",
            "next_best_action=": "inspect report/logs and retry",
        }
    for key in keys:
        if not has[key]:
            ui_findings.append(f"{key}{defaults[key]}")
    if not any(str(item).startswith("final_state=") for item in ui_findings):
        ui_findings.append(f"final_state={result}")


def finalize_result(
    *,
    result: str,
    force_keep_open: bool,
    console_errors: list[str],
    network_findings: list[str],
    verified: bool,
    steps_count: int,
    ui_findings: list[str],
    where_default: str,
) -> str:
    out = result
    if force_keep_open:
        ui_findings.append("teaching handoff: browser kept open for manual control")
    if out != "failed" and (console_errors or network_findings):
        out = "partial"
    if verified and steps_count > 0 and not ui_findings:
        out = "failed"
        ui_findings.append("what_failed=verified_mode_missing_findings")
        ui_findings.append("where=post-run")
        ui_findings.append("why_likely=verified mode requires explicit visible verification findings")
        ui_findings.append("attempted=verified post-check")
        ui_findings.append("next_best_action=add verify visible result findings")
    ensure_structured_ui_findings(ui_findings, result=out, where_default=where_default)
    return out
