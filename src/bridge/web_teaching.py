"""Teaching-mode helpers for manual handoff and learning capture."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def show_teaching_handoff_notice(page: Any, target: str) -> None:
    msg = f"No encuentro el botón: {target}. Te cedo el control."
    try:
        page.evaluate(
            """
            ([message]) => {
              const id = '__bridge_teaching_handoff_notice';
              let el = document.getElementById(id);
              if (!el) {
                el = document.createElement('div');
                el.id = id;
                el.style.position = 'fixed';
                el.style.left = '50%';
                el.style.bottom = '18px';
                el.style.transform = 'translateX(-50%)';
                el.style.padding = '10px 14px';
                el.style.borderRadius = '10px';
                el.style.background = 'rgba(245,158,11,0.95)';
                el.style.color = '#fff';
                el.style.font = '13px/1.3 monospace';
                el.style.zIndex = '2147483647';
                el.style.boxShadow = '0 8px 18px rgba(0,0,0,0.3)';
                document.documentElement.appendChild(el);
              }
              el.textContent = String(message || '');
            }
            """,
            [msg],
        )
    except Exception:
        return


def show_learning_thanks_notice(page: Any, target: str) -> None:
    label = target or "ese control"
    msg = f"Gracias, ya he aprendido dónde está {label}. Ya continúo yo."
    try:
        page.evaluate(
            """
            ([message]) => {
              const id = '__bridge_teaching_handoff_notice';
              let el = document.getElementById(id);
              if (!el) {
                el = document.createElement('div');
                el.id = id;
                el.style.position = 'fixed';
                el.style.left = '50%';
                el.style.bottom = '18px';
                el.style.transform = 'translateX(-50%)';
                el.style.padding = '10px 14px';
                el.style.borderRadius = '10px';
                el.style.background = 'rgba(16,185,129,0.96)';
                el.style.color = '#fff';
                el.style.font = '13px/1.3 monospace';
                el.style.zIndex = '2147483647';
                el.style.boxShadow = '0 8px 18px rgba(0,0,0,0.3)';
                document.documentElement.appendChild(el);
              }
              el.textContent = String(message || '');
            }
            """,
            [msg],
        )
    except Exception:
        return


def normalize_failed_target_label(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    return text.split(":")[-1].strip().strip("'\"")


def show_wrong_manual_click_notice(
    page: Any, failed_target: str, stable_selectors_for_target: Callable[[str], list[str]]
) -> None:
    label = normalize_failed_target_label(failed_target) or "objetivo esperado"
    suggestion = stable_selectors_for_target(label)
    hint = suggestion[0] if suggestion else label
    msg = f"Ese click no coincide. El objetivo es '{label}'. Prueba con: {hint}"
    try:
        page.evaluate(
            """
            ([message]) => {
              const id = '__bridge_teaching_handoff_notice';
              let el = document.getElementById(id);
              if (!el) {
                el = document.createElement('div');
                el.id = id;
                el.style.position = 'fixed';
                el.style.left = '50%';
                el.style.bottom = '18px';
                el.style.transform = 'translateX(-50%)';
                el.style.padding = '10px 14px';
                el.style.borderRadius = '10px';
                el.style.background = 'rgba(239,68,68,0.96)';
                el.style.color = '#fff';
                el.style.font = '13px/1.3 monospace';
                el.style.zIndex = '2147483647';
                el.style.boxShadow = '0 8px 18px rgba(0,0,0,0.3)';
                document.documentElement.appendChild(el);
              }
              el.textContent = String(message || '');
            }
            """,
            [msg],
        )
    except Exception:
        return


def is_relevant_manual_learning_event(evt: dict[str, Any], failed_target: str) -> bool:
    selector = str(evt.get("selector", "")).strip().lower()
    target = str(evt.get("target", "")).strip().lower()
    text = str(evt.get("text", "")).strip().lower()
    message = str(evt.get("message", "")).strip().lower()

    if "__bridge_" in selector:
        return False
    if target in {"release", "close", "refresh", "clear incident", "ack"}:
        return False

    raw = str(failed_target or "").strip().lower()
    if not raw:
        return True
    probe = raw.split(":")[-1].strip().strip("'\"")
    if not probe:
        return True
    if probe.startswith("#") and probe in selector:
        return True
    token = re.sub(r"[^a-z0-9]+", " ", probe).strip()
    if not token:
        return True
    if token in selector or token in target or token in text or token in message:
        return True
    parts = [p for p in token.split() if len(p) >= 3]
    if parts and any(p in selector for p in parts) and ("stop" in parts or "play" in parts):
        return True
    return False


def capture_manual_learning(
    *,
    page: Any | None,
    session: Any,
    failed_target: str,
    context: dict[str, str],
    wait_seconds: int,
    request_session_state: Callable[[Any], dict[str, Any]],
    show_wrong_click_notice: Callable[[Any, str], None],
) -> dict[str, Any] | None:
    max_wait = max(4, min(180, int(wait_seconds)))
    deadline = datetime.now(timezone.utc).timestamp() + max_wait
    seen: set[str] = set()
    while datetime.now(timezone.utc).timestamp() < deadline:
        try:
            state = request_session_state(session)
        except BaseException:
            return None
        events = list(state.get("recent_events", []) or [])
        for evt in reversed(events):
            if not isinstance(evt, dict):
                continue
            key = "|".join(
                [
                    str(evt.get("created_at", "")),
                    str(evt.get("type", "")),
                    str(evt.get("message", "")),
                ]
            )
            if key in seen:
                continue
            seen.add(key)
            if str(evt.get("type", "")).strip().lower() != "click":
                continue
            if not is_relevant_manual_learning_event(evt, failed_target):
                if page is not None:
                    show_wrong_click_notice(page, failed_target)
                continue
            selector = str(evt.get("selector", "")).strip()
            target = str(evt.get("target", "")).strip()
            return {
                "failed_target": failed_target or target,
                "selector": selector,
                "target": target,
                "timestamp": str(evt.get("created_at", "")),
                "url": str(evt.get("url", "")),
                "state_key": context.get("state_key", ""),
            }
        try:
            from time import sleep

            sleep(0.7)
        except Exception:
            break
    return None


def write_teaching_artifacts(
    run_dir: Path,
    payload: dict[str, Any],
    to_repo_rel: Callable[[Path], str],
) -> list[str]:
    out_dir = run_dir / "learning"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"teaching_{stamp}"
    json_path = out_dir / f"{base}.json"
    md_path = out_dir / f"{base}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_lines = [
        "# Teaching Artifact",
        "",
        f"- failed_target: `{payload.get('failed_target', '')}`",
        f"- selector: `{payload.get('selector', '')}`",
        f"- click_target_text: `{payload.get('target', '')}`",
        f"- timestamp: `{payload.get('timestamp', '')}`",
        f"- url: `{payload.get('url', '')}`",
        f"- state_key: `{payload.get('state_key', '')}`",
    ]
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return [to_repo_rel(json_path), to_repo_rel(md_path)]


def resume_after_learning(
    *,
    page: Any,
    selector: str,
    target: str,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
) -> bool:
    sel = str(selector or "").strip()
    if not sel:
        return False
    try:
        locator = page.locator(sel).first
        locator.wait_for(state="visible", timeout=3500)
        locator.click(timeout=3500)
        actions.append(f"cmd: playwright click selector:{sel} (learning-resume)")
        observations.append(f"learning-resume clicked selector: {sel}")
        ui_findings.append(f"learning_resume=success target={target}")
        return True
    except Exception:
        return False
