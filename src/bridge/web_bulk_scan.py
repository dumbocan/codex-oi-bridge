"""DOM scanning helpers for bulk web actions."""

from __future__ import annotations

from typing import Any


def selected_playlist_name(page: Any) -> str:
    try:
        raw = page.evaluate(
            """
            () => {
              const sel = document.querySelector('#playlist-select');
              if (!sel) return '';
              const selected = sel.options?.[sel.selectedIndex];
              return String(selected?.textContent || '').trim();
            }
            """
        )
        return str(raw or "").strip()
    except Exception:
        return ""


def scan_visible_ready_add_selectors(page: Any, seen: set[str]) -> tuple[list[str], bool]:
    try:
        data = page.evaluate(
            """
            (seenSelectors) => {
              const cards = Array.from(document.querySelectorAll('.track-card'));
              const out = [];
              let reachedBottom = false;
              const vh = window.innerHeight || 0;
              for (const card of cards) {
                const r = card.getBoundingClientRect();
                const visible = r.height > 0 && r.bottom > 0 && r.top < vh;
                if (!visible) continue;
                const readyBadge = Array.from(card.querySelectorAll('*')).some((el) => {
                  const txt = String(el.textContent || '').trim().toUpperCase();
                  return txt === 'READY';
                });
                if (!readyBadge) continue;
                const addBtn = card.querySelector(
                  '[id^="track-add-to-playlist-"], [data-testid^="track-add-to-playlist-"]'
                );
                if (!addBtn) continue;
                const disabled = !!(addBtn.disabled || addBtn.getAttribute('aria-disabled') === 'true');
                if (disabled) continue;
                const id = String(addBtn.id || '').trim();
                const testid = String(addBtn.getAttribute('data-testid') || '').trim();
                let selector = '';
                if (id) selector = `#${id}`;
                else if (testid) selector = `[data-testid="${testid}"]`;
                if (!selector) continue;
                if (seenSelectors.includes(selector)) continue;
                out.push(selector);
              }
              const maxY = Math.max(
                0,
                (document.documentElement?.scrollHeight || 0) - (window.innerHeight || 0)
              );
              reachedBottom = (window.scrollY || 0) >= (maxY - 2);
              return { selectors: out, reachedBottom };
            }
            """,
            list(seen),
        )
        if not isinstance(data, dict):
            return [], False
        selectors = [str(item).strip() for item in (data.get("selectors") or []) if str(item).strip()]
        reached_bottom = bool(data.get("reachedBottom", False))
        return selectors, reached_bottom
    except Exception:
        return [], False


def scan_playlist_remove_selectors(page: Any, seen: set[str]) -> tuple[list[str], bool]:
    try:
        data = page.evaluate(
            """
            (seenSelectors) => {
              const rows = Array.from(
                document.querySelectorAll('[id^="playlist-track-row-"], .playlist-track-row')
              );
              const out = [];
              for (const row of rows) {
                const btn = row.querySelector(
                  '[id^="playlist-track-remove-"], [data-testid^="playlist-track-remove-"]'
                );
                if (!btn) continue;
                const id = String(btn.id || '').trim();
                const testid = String(btn.getAttribute('data-testid') || '').trim();
                let selector = '';
                if (id) selector = `#${id}`;
                else if (testid) selector = `[data-testid="${testid}"]`;
                if (!selector) continue;
                if (seenSelectors.includes(selector)) continue;
                out.push(selector);
              }
              const done = out.length === 0;
              return { selectors: out, done };
            }
            """,
            list(seen),
        )
        if not isinstance(data, dict):
            return [], False
        selectors = [str(item).strip() for item in (data.get("selectors") or []) if str(item).strip()]
        done = bool(data.get("done", False))
        return selectors, done
    except Exception:
        return [], False


def scan_visible_buttons_in_cards(
    page: Any,
    *,
    card_selector: str,
    button_selector: str,
    required_text: str,
    seen: set[str],
) -> tuple[list[str], bool]:
    try:
        data = page.evaluate(
            """
            ({cardSelector, buttonSelector, requiredText, seenSelectors}) => {
              const cards = Array.from(document.querySelectorAll(String(cardSelector || '.track-card')));
              const out = [];
              const vh = window.innerHeight || 0;
              const need = String(requiredText || '').trim().toLowerCase();
              for (const card of cards) {
                const r = card.getBoundingClientRect();
                const visible = r.height > 0 && r.bottom > 0 && r.top < vh;
                if (!visible) continue;
                const text = String(card.textContent || '').toLowerCase();
                if (need && !text.includes(need)) continue;
                const btn = card.querySelector(String(buttonSelector || 'button'));
                if (!btn) continue;
                const disabled = !!(btn.disabled || btn.getAttribute('aria-disabled') === 'true');
                if (disabled) continue;
                const id = String(btn.id || '').trim();
                const testid = String(btn.getAttribute('data-testid') || '').trim();
                let selector = '';
                if (id) selector = `#${id}`;
                else if (testid) selector = `[data-testid="${testid}"]`;
                if (!selector) continue;
                if (seenSelectors.includes(selector)) continue;
                out.push(selector);
              }
              const maxY = Math.max(
                0,
                (document.documentElement?.scrollHeight || 0) - (window.innerHeight || 0)
              );
              const reachedBottom = (window.scrollY || 0) >= (maxY - 2);
              return { selectors: out, reachedBottom };
            }
            """,
            {
                "cardSelector": card_selector,
                "buttonSelector": button_selector,
                "requiredText": required_text,
                "seenSelectors": list(seen),
            },
        )
        if not isinstance(data, dict):
            return [], False
        selectors = [str(item).strip() for item in (data.get("selectors") or []) if str(item).strip()]
        reached_bottom = bool(data.get("reachedBottom", False))
        return selectors, reached_bottom
    except Exception:
        return [], False


def scan_visible_selectors(page: Any, *, button_selector: str, seen: set[str]) -> list[str]:
    try:
        data = page.evaluate(
            """
            ({buttonSelector, seenSelectors}) => {
              const nodes = Array.from(document.querySelectorAll(String(buttonSelector || 'button')));
              const out = [];
              const vh = window.innerHeight || 0;
              for (const btn of nodes) {
                const r = btn.getBoundingClientRect();
                const visible = r.height > 0 && r.bottom > 0 && r.top < vh;
                if (!visible) continue;
                const disabled = !!(btn.disabled || btn.getAttribute('aria-disabled') === 'true');
                if (disabled) continue;
                const id = String(btn.id || '').trim();
                const testid = String(btn.getAttribute('data-testid') || '').trim();
                let selector = '';
                if (id) selector = `#${id}`;
                else if (testid) selector = `[data-testid="${testid}"]`;
                if (!selector) continue;
                if (seenSelectors.includes(selector)) continue;
                out.push(selector);
              }
              return out;
            }
            """,
            {"buttonSelector": button_selector, "seenSelectors": list(seen)},
        )
        if not isinstance(data, list):
            return []
        return [str(item).strip() for item in data if str(item).strip()]
    except Exception:
        return []
