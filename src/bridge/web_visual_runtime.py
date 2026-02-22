"""Runtime helpers for visual overlay robustness in web-run."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from bridge.web_visual_overlay import (
    _ensure_visual_overlay_installed,
    _read_visual_overlay_snapshot,
    _verify_visual_overlay_visible,
)


def force_visual_overlay_reinstall(page: Any) -> None:
    page.evaluate(
        """
        () => {
          const ids = [
            '__bridge_cursor_overlay',
            '__bridge_trail_layer',
            '__bridge_state_border',
            '__bridge_step_badge',
          ];
          ids.forEach((id) => document.getElementById(id)?.remove());
          window.__bridgeOverlayInstalled = false;
          if (typeof window.__bridgeEnsureOverlay === 'function') {
            window.__bridgeEnsureOverlay();
          }
        }
        """
    )


def ensure_visual_overlay_ready(page: Any, retries: int = 12, delay_ms: int = 120) -> None:
    last_error: BaseException | None = None
    for _ in range(max(1, retries)):
        try:
            _ensure_visual_overlay_installed(page)
            _verify_visual_overlay_visible(page)
            return
        except BaseException as exc:
            last_error = exc
            try:
                page.wait_for_timeout(delay_ms)
            except Exception:
                pass
    if isinstance(last_error, BaseException):
        raise RuntimeError(str(last_error))
    raise RuntimeError("Visual overlay not visible after retries.")


def ensure_visual_overlay_ready_best_effort(
    page: Any,
    ui_findings: list[str],
    *,
    cursor_expected: bool,
    retries: int,
    delay_ms: int,
    debug_screenshot_path: Path | None = None,
    force_reinit: bool = False,
    to_repo_rel: Callable[[Path], str],
) -> bool:
    # Force re-injection / re-enable in attach flows and after navigations.
    last_error: BaseException | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            if force_reinit:
                try:
                    force_visual_overlay_reinstall(page)
                except BaseException as reinstall_exc:
                    last_error = reinstall_exc
            _ensure_visual_overlay_installed(page)
            if cursor_expected:
                try:
                    _verify_visual_overlay_visible(page)
                    return True
                except BaseException as exc:
                    last_error = exc
                    try:
                        force_visual_overlay_reinstall(page)
                    except BaseException as reinstall_exc:
                        last_error = reinstall_exc
            else:
                return True
        except BaseException as exc:
            last_error = exc
        try:
            page.wait_for_timeout(delay_ms)
        except Exception:
            pass
        ui_findings.append(f"visual overlay retry {attempt}/{retries}")

    snapshot = _read_visual_overlay_snapshot(page)
    ui_findings.append(f"visual overlay snapshot: {snapshot}")
    if debug_screenshot_path is not None:
        try:
            page.screenshot(path=str(debug_screenshot_path), full_page=True)
            ui_findings.append(f"visual overlay debug screenshot: {to_repo_rel(debug_screenshot_path)}")
        except Exception as screenshot_exc:
            ui_findings.append(f"visual overlay debug screenshot failed: {screenshot_exc}")
    ui_findings.append(
        "visual overlay degraded: cursor overlay not visible; continuing without cursor"
    )
    if last_error is not None:
        ui_findings.append(f"visual overlay error: {last_error}")
    return False
