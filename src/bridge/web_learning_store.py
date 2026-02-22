"""Persistence and selector-prioritization helpers for web teaching mode."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def load_learned_selectors(learning_json: Path) -> dict[str, dict[str, list[str]]]:
    try:
        if not learning_json.exists():
            return {}
        payload = json.loads(learning_json.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        entry: dict[str, list[str]] = {}
        for tgt, selectors in value.items():
            if isinstance(tgt, str) and isinstance(selectors, list):
                entry[tgt] = [str(s).strip() for s in selectors if str(s).strip()]
        if entry:
            out[str(key)] = entry
    return out


def write_learning_audit(
    *,
    learning_dir: Path,
    target: str,
    selector: str,
    context: dict[str, str],
    source: str,
) -> None:
    audit = learning_dir / "web_teaching_audit.md"
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        f"- {now} target=`{target}` selector=`{selector}` source=`{source}`",
        f"  - context: {context.get('state_key', '')}",
    ]
    with audit.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def normalize_learning_target_key(
    raw: str,
    *,
    selector: str = "",
    normalize_failed_target_label: Callable[[str], str],
) -> str:
    text = str(raw or "").strip().lower()
    sel = str(selector or "").strip().lower()
    probe = normalize_failed_target_label(text).lower() or text
    merged = " ".join([text, probe, sel]).strip()
    if not merged:
        return ""
    if text.startswith("step ") and ("click_" in text or "wait_" in text):
        return ""
    cleaned = re.sub(r"[^a-z0-9]+", " ", probe).strip()
    if not cleaned:
        return ""
    return cleaned[:48]


def is_learning_target_candidate(target: str) -> bool:
    text = str(target or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith("step ") and ("wait_" in lowered or "click_" in lowered):
        return False
    return True


def is_specific_selector(selector: str) -> bool:
    low = str(selector or "").strip().lower()
    if not low:
        return False
    if ":has-text(" in low:
        return False
    return "__bridge_" not in low


def store_learned_selector(
    *,
    learning_dir: Path,
    learning_json: Path,
    target: str,
    selector: str,
    context: dict[str, str],
    source: str,
    normalize_failed_target_label: Callable[[str], str],
) -> None:
    target_norm = normalize_learning_target_key(
        target,
        selector=selector,
        normalize_failed_target_label=normalize_failed_target_label,
    )
    selector_norm = str(selector).strip()
    if not target_norm or not selector_norm:
        return
    if not is_specific_selector(selector_norm):
        return
    all_map = load_learned_selectors(learning_json)
    state_key = str(context.get("state_key", "")).strip()
    if not state_key:
        return
    state_bucket = all_map.setdefault(state_key, {})
    selectors = state_bucket.setdefault(target_norm, [])
    if selector_norm in selectors:
        return
    selectors.insert(0, selector_norm)
    state_bucket[target_norm] = selectors[:6]
    learning_dir.mkdir(parents=True, exist_ok=True)
    learning_json.write_text(json.dumps(all_map, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_learning_audit(
        learning_dir=learning_dir,
        target=target_norm,
        selector=selector_norm,
        context=context,
        source=source,
    )


def learned_selectors_for_step(
    *,
    step: Any,
    selector_map: dict[str, dict[str, list[str]]],
    context: dict[str, str],
    normalize_failed_target_label: Callable[[str], str],
) -> list[str]:
    if getattr(step, "kind", "") not in {"click_text", "click_selector"}:
        return []
    state_key = str(context.get("state_key", "")).strip()
    if not state_key:
        return []
    bucket = selector_map.get(state_key, {})
    raw_key = str(getattr(step, "target", "")).strip().lower()
    norm_key = normalize_learning_target_key(
        str(getattr(step, "target", "")),
        normalize_failed_target_label=normalize_failed_target_label,
    )
    out: list[str] = []
    for key in (norm_key, raw_key):
        if not key:
            continue
        for selector in bucket.get(key, []):
            if not is_specific_selector(selector):
                continue
            if (
                getattr(step, "kind", "") == "click_selector"
                and str(getattr(step, "target", "")).strip()
                and selector != str(getattr(step, "target", "")).strip()
            ):
                continue
            if selector not in out:
                out.append(selector)
    return out


def prioritize_steps_with_learned_selectors(
    *,
    steps: list[Any],
    selector_map: dict[str, dict[str, list[str]]],
    context: dict[str, str],
    normalize_failed_target_label: Callable[[str], str],
    step_factory: Callable[[str, str], Any],
) -> list[Any]:
    if not steps:
        return steps
    out: list[Any] = []
    for step in steps:
        out.append(step)
        learned = learned_selectors_for_step(
            step=step,
            selector_map=selector_map,
            context=context,
            normalize_failed_target_label=normalize_failed_target_label,
        )
        if getattr(step, "kind", "") == "click_text" and learned:
            out.pop()
            for selector in learned:
                out.append(step_factory("click_selector", selector))
            out.append(step)
    return out
