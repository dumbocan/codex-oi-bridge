"""Deterministic web interaction backend using Playwright."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bridge.models import OIReport


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_CLICK_TEXT_RE = re.compile(
    r"(?:click|haz\s+click|pulsa|presiona)[^\"'<>]{0,80}[\"'“”]([^\"'“”]{1,120})[\"'“”]",
    flags=re.IGNORECASE,
)
_SELECTOR_RE = re.compile(
    r"selector\s*[=:]?\s*[\"'“”]([^\"'“”]{1,160})[\"'“”]",
    flags=re.IGNORECASE,
)


def run_web_task(task: str, run_dir: Path, timeout_seconds: int, verified: bool = False) -> OIReport:
    url_match = _URL_RE.search(task)
    if not url_match:
        raise SystemExit("Web mode requires an explicit URL in task.")
    url = _normalize_url(url_match.group(0))
    if not _is_valid_url(url):
        raise SystemExit(f"Web mode received invalid URL token: {url_match.group(0)}")
    click_texts = [m.group(1).strip() for m in _CLICK_TEXT_RE.finditer(task)]
    selectors = [m.group(1).strip() for m in _SELECTOR_RE.finditer(task)]
    steps = _build_steps(click_texts, selectors)

    if not _playwright_available():
        raise SystemExit(
            "Playwright Python package is not installed. "
            "Install it in the environment to use --mode web."
        )

    return _execute_playwright(url, steps, run_dir, timeout_seconds, verified=verified)


def _build_steps(click_texts: list[str], selectors: list[str]) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = []
    for selector in selectors:
        steps.append({"kind": "selector", "value": selector})
    for text in click_texts:
        steps.append({"kind": "text", "value": text})
    return steps


def _playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _execute_playwright(
    url: str,
    steps: list[dict[str, str]],
    run_dir: Path,
    timeout_seconds: int,
    *,
    verified: bool,
) -> OIReport:
    from playwright.sync_api import sync_playwright

    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    actions: list[str] = [f"cmd: playwright goto {url}"]
    observations: list[str] = []
    console_errors: list[str] = []
    network_findings: list[str] = []
    ui_findings: list[str] = []
    evidence_paths: list[str] = []

    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_page()
        page.set_default_timeout(min(timeout_seconds * 1000, 120000))

        def on_console(msg: Any) -> None:
            if msg.type == "error":
                console_errors.append(msg.text)

        def on_response(resp: Any) -> None:
            try:
                if resp.status >= 400:
                    network_findings.append(f"{resp.request.method} {resp.url} {resp.status}")
            except Exception:
                pass

        def on_failed(req: Any) -> None:
            failure = req.failure
            text = failure.get("errorText") if isinstance(failure, dict) else str(failure)
            network_findings.append(f"FAILED {req.method} {req.url} {text}")

        page.on("console", on_console)
        page.on("response", on_response)
        page.on("requestfailed", on_failed)

        page.goto(url, wait_until="domcontentloaded")
        observations.append(f"Opened URL: {url}")
        observations.append(f"Page title: {page.title()}")

        for idx, step in enumerate(steps, start=1):
            before = evidence_dir / f"step_{idx}_before.png"
            after = evidence_dir / f"step_{idx}_after.png"
            page.screenshot(path=str(before), full_page=True)
            evidence_paths.append(_to_repo_rel(before))

            if step["kind"] == "selector":
                actions.append(f"cmd: playwright click selector:{step['value']}")
                page.locator(step["value"]).first.click()
                observations.append(f"Clicked selector in step {idx}: {step['value']}")
            else:
                actions.append(f"cmd: playwright click text:{step['value']}")
                page.get_by_text(step["value"], exact=False).first.click()
                observations.append(f"Clicked text in step {idx}: {step['value']}")

            page.wait_for_timeout(1000)
            page.screenshot(path=str(after), full_page=True)
            evidence_paths.append(_to_repo_rel(after))
            ui_findings.append(
                f"step {idx} verify visible result: url={page.url}, title={page.title()}"
            )

        browser.close()

    result = "success"
    if console_errors or network_findings:
        result = "partial"
    if verified and steps and not ui_findings:
        raise SystemExit("Verified web mode requires post-step visible verification findings.")

    return OIReport(
        task_id=run_dir.name,
        goal=f"web: {url}",
        actions=actions,
        observations=observations,
        console_errors=console_errors,
        network_findings=network_findings,
        ui_findings=ui_findings,
        result=result,
        evidence_paths=evidence_paths,
    )


def _launch_browser(playwright_obj: Any) -> Any:
    try:
        return playwright_obj.chromium.launch(headless=True, channel="chrome")
    except Exception:
        return playwright_obj.chromium.launch(headless=True)


def _to_repo_rel(path: Path) -> str:
    return str(path.resolve().relative_to(Path.cwd()))


def _normalize_url(raw: str) -> str:
    return raw.rstrip(".,;:!?)]}\"'")


def _is_valid_url(text: str) -> bool:
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
