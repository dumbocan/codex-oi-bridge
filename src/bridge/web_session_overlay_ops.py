"""Session-level overlay/top-bar operations for attached browser sessions."""

from __future__ import annotations

from typing import Any, Callable


def release_session_control_overlay(
    session: Any,
    *,
    set_assistant_control_overlay: Callable[[Any, bool], None],
    update_top_bar_state: Callable[[Any, dict[str, Any]], None],
    session_state_payload: Callable[..., dict[str, Any]],
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{session.port}")
        except Exception:
            return
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            return
        page = context.pages[0] if context.pages else None
        if page is None:
            return
        try:
            set_assistant_control_overlay(page, False)
            update_top_bar_state(page, session_state_payload(session, override_controlled=False))
        except Exception:
            return


def destroy_session_top_bar(
    session: Any,
    *,
    destroy_top_bar: Callable[[Any], None],
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{session.port}")
        except Exception:
            return
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            return
        page = context.pages[0] if context.pages else None
        if page is None:
            return
        try:
            destroy_top_bar(page)
        except Exception:
            return


def ensure_session_top_bar(
    session: Any,
    *,
    install_visual_overlay: Callable[..., None],
    set_assistant_control_overlay: Callable[[Any, bool], None],
    update_top_bar_state: Callable[[Any, dict[str, Any]], None],
    session_state_payload: Callable[..., dict[str, Any]],
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{session.port}")
        except Exception:
            return
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        try:
            install_visual_overlay(
                page,
                cursor_enabled=False,
                click_pulse_enabled=False,
                scale=1.0,
                color="#3BA7FF",
                trace_enabled=False,
                session_state=session_state_payload(session),
            )
            set_assistant_control_overlay(page, bool(session.controlled))
            update_top_bar_state(page, session_state_payload(session))
        except Exception:
            return
