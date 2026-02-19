"""Frame/focus guard helpers for main-frame-first web interactions."""

from __future__ import annotations

import time
from typing import Any, Callable


def is_iframe_focus_locked(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const active = document.activeElement;
                  if (!active) return false;
                  if (String(active.tagName || '').toUpperCase() === 'IFRAME') return true;
                  return !!document.querySelector('iframe:focus,iframe:focus-within');
                }
                """
            )
        )
    except Exception:
        return False


def disable_active_youtube_iframe_pointer_events(
    page: Any,
    *,
    page_is_closed: Callable[[Any | None], bool],
) -> dict[str, Any] | None:
    if page_is_closed(page):
        return None
    try:
        token = page.evaluate(
            """
            () => {
              const active = document.activeElement;
              let frame = null;
              if (active && String(active.tagName || '').toUpperCase() === 'IFRAME') {
                frame = active;
              }
              if (!frame) frame = document.querySelector('iframe:focus,iframe:focus-within');
              if (!frame) return null;
              const src = String(frame.getAttribute('src') || '').toLowerCase();
              const isYoutube =
                src.includes('youtube.com') ||
                src.includes('youtube-nocookie.com') ||
                src.includes('youtu.be');
              if (!isYoutube) return null;
              const prev = String(frame.style.pointerEvents || '');
              frame.setAttribute('data-bridge-prev-pe', prev || '__EMPTY__');
              frame.style.pointerEvents = 'none';
              const all = Array.from(document.querySelectorAll('iframe'));
              const idx = all.indexOf(frame);
              return { idx, id: String(frame.id || ''), prev };
            }
            """
        )
    except Exception:
        return None
    return token if isinstance(token, dict) else None


def restore_iframe_pointer_events(
    page: Any,
    token: dict[str, Any] | None,
    *,
    page_is_closed: Callable[[Any | None], bool],
) -> None:
    if not token or page_is_closed(page):
        return
    try:
        page.evaluate(
            """
            ([tok]) => {
              if (!tok || typeof tok !== 'object') return;
              const all = Array.from(document.querySelectorAll('iframe'));
              let frame = null;
              if (tok.id) frame = document.getElementById(String(tok.id));
              if (!frame && Number.isInteger(tok.idx) && tok.idx >= 0 && tok.idx < all.length) {
                frame = all[tok.idx];
              }
              if (!frame) return;
              const prevAttr = frame.getAttribute('data-bridge-prev-pe');
              const prev = prevAttr === '__EMPTY__' ? '' : String(prevAttr || tok.prev || '');
              frame.style.pointerEvents = prev;
              frame.removeAttribute('data-bridge-prev-pe');
            }
            """,
            [token],
        )
    except Exception:
        return


def force_main_frame_context(
    page: Any,
    *,
    max_seconds: float,
    iframe_focus_locked: Callable[[Any], bool],
) -> bool:
    deadline = time.monotonic() + max(0.1, float(max_seconds))
    while time.monotonic() <= deadline:
        try:
            page.evaluate(
                """
                () => {
                  const active = document.activeElement;
                  if (active && String(active.tagName || '').toUpperCase() === 'IFRAME') {
                    try { active.blur(); } catch (_e) {}
                  }
                }
                """
            )
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            page.evaluate(
                """
                () => {
                  if (!document.body) return false;
                  if (typeof document.body.focus === 'function') document.body.focus();
                  try {
                    const evt = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
                    document.body.dispatchEvent(evt);
                  } catch (_e) {}
                  return true;
                }
                """
            )
        except Exception:
            pass
        try:
            is_main = bool(page.evaluate("() => !!document.body && window === window.top"))
        except Exception:
            is_main = False
        if is_main and not iframe_focus_locked(page):
            return True
        try:
            page.wait_for_timeout(120)
        except Exception:
            pass
    return False
