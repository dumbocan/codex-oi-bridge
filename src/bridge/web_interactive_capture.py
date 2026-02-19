"""Helpers for interactive visual capture and whole-page scanning."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def capture_movement(
    *,
    page: Any,
    tag: str,
    step_num: int,
    move_capture_count: int,
    visual: bool,
    movement_capture_dir: Path | None,
    evidence_paths: list[str] | None,
    get_last_human_route: Callable[[], list[tuple[float, float]]],
    to_repo_rel: Callable[[Path], str],
) -> int:
    if not visual:
        return move_capture_count
    if movement_capture_dir is None or evidence_paths is None:
        return move_capture_count
    move_capture_count += 1
    shot = movement_capture_dir / f"step_{step_num}_move_{move_capture_count}_{tag}.png"
    try:
        pts = get_last_human_route()
        vw_vh = page.evaluate("() => ({w: window.innerWidth || 1280, h: window.innerHeight || 860})")
        if isinstance(pts, list) and len(pts) >= 2:
            w = int((vw_vh or {}).get("w") or 1280)
            h = int((vw_vh or {}).get("h") or 860)
            clean_pts: list[tuple[float, float]] = []
            for p in pts:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    try:
                        clean_pts.append((float(p[0]), float(p[1])))
                    except Exception:
                        continue
            if len(clean_pts) >= 2:
                svg_path = movement_capture_dir / f"step_{step_num}_move_{move_capture_count}_{tag}.svg"
                points_attr = " ".join(f"{x:.2f},{y:.2f}" for x, y in clean_pts)
                svg = (
                    f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
                    f'viewBox="0 0 {w} {h}">'
                    f'<polyline fill="none" stroke="rgb(0,180,255)" stroke-width="6" '
                    f'stroke-linecap="round" stroke-linejoin="round" points="{points_attr}" />'
                    "</svg>\n"
                )
                svg_path.write_text(svg, encoding="utf-8")
                evidence_paths.append(to_repo_rel(svg_path))
        page.evaluate(
            """
            () => {
              const prev = document.getElementById('__bridge_capture_path');
              if (prev) prev.remove();
              const pts = window.__bridgeLastHumanRoute;
              if (!Array.isArray(pts) || pts.length < 2) return;
              const clean = pts
                .map((p) => Array.isArray(p) ? { x: Number(p[0]), y: Number(p[1]) } : null)
                .filter((p) => p && Number.isFinite(p.x) && Number.isFinite(p.y));
              if (clean.length < 2) return;
              const svgNS = 'http://www.w3.org/2000/svg';
              const svg = document.createElementNS(svgNS, 'svg');
              svg.id = '__bridge_capture_path';
              svg.setAttribute('width', '100%');
              svg.setAttribute('height', '100%');
              svg.setAttribute(
                'viewBox',
                `0 0 ${Math.max(1, window.innerWidth || 1)} ${Math.max(1, window.innerHeight || 1)}`
              );
              svg.setAttribute('preserveAspectRatio', 'none');
              svg.style.position = 'fixed';
              svg.style.inset = '0';
              svg.style.pointerEvents = 'none';
              svg.style.zIndex = '2147483646';
              const poly = document.createElementNS(svgNS, 'polyline');
              poly.setAttribute('fill', 'none');
              poly.setAttribute('stroke', 'rgba(0,180,255,1)');
              poly.setAttribute('stroke-width', '8');
              poly.setAttribute('stroke-linecap', 'round');
              poly.setAttribute('stroke-linejoin', 'round');
              poly.setAttribute('points', clean.map((p) => `${p.x},${p.y}`).join(' '));
              svg.appendChild(poly);
              document.documentElement.appendChild(svg);
            }
            """
        )
        page.wait_for_timeout(50)
        page.screenshot(path=str(shot), full_page=False)
        page.evaluate("() => document.getElementById('__bridge_capture_path')?.remove()")
        evidence_paths.append(to_repo_rel(shot))
    except Exception:
        return move_capture_count
    return move_capture_count


def scan_whole_page_for_play_buttons(page: Any) -> int:
    # Force full-page scan before generic play clicks.
    total = 0
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass
    for _ in range(18):
        try:
            total = int(
                page.evaluate(
                    """
                    () => document.querySelectorAll(
                      "[id^='track-play-'], [data-testid^='track-play-'], .track-card button"
                    ).length
                    """
                )
            )
        except Exception:
            total = 0
        try:
            moved = bool(
                page.evaluate(
                    """
                    () => {
                      const maxY = Math.max(
                        0,
                        (document.documentElement?.scrollHeight || 0) - window.innerHeight
                      );
                      const prev = window.scrollY || 0;
                      const next = Math.min(maxY, prev + Math.max(130, Math.floor(window.innerHeight * 0.28)));
                      window.scrollTo(0, next);
                      return next > prev;
                    }
                    """
                )
            )
        except Exception:
            moved = False
        if not moved:
            break
        try:
            page.wait_for_timeout(95)
        except Exception:
            pass
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass
    return total
