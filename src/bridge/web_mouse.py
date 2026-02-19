"""Human-like mouse movement helpers for visual web-run."""

from __future__ import annotations

import math
import random
import time
from typing import Any

_LAST_HUMAN_ROUTE: list[tuple[float, float]] = []


def get_last_human_route() -> list[tuple[float, float]]:
    return list(_LAST_HUMAN_ROUTE)


def _human_mouse_move(page: Any, x: float, y: float, *, speed: float) -> None:
    # Humanized path: elliptical sway + noisy lateral drift + clear deceleration near target.
    global _LAST_HUMAN_ROUTE
    try:
        viewport = page.evaluate("() => ({w: window.innerWidth || 0, h: window.innerHeight || 0})")
        vw = float((viewport or {}).get("w") or 0)
        vh = float((viewport or {}).get("h") or 0)
    except Exception:
        vw = 0.0
        vh = 0.0

    def _clamp(px: float, py: float) -> tuple[float, float]:
        if vw > 0:
            px = max(0.0, min(vw - 1.0, px))
        if vh > 0:
            py = max(0.0, min(vh - 1.0, py))
        return px, py

    target_x, target_y = _clamp(float(x), float(y))
    start_x, start_y = target_x, target_y
    has_cursor_pos = False
    try:
        pos = page.evaluate("() => window.__bridgeGetCursorPos?.() || null")
        if isinstance(pos, dict):
            sx = pos.get("x")
            sy = pos.get("y")
            if isinstance(sx, (int, float)) and isinstance(sy, (int, float)):
                start_x, start_y = _clamp(float(sx), float(sy))
                has_cursor_pos = True
    except Exception:
        pass
    if not has_cursor_pos and vw > 0 and vh > 0:
        start_x, start_y = _clamp(vw * 0.5, vh * 0.5)

    rng = random.Random(int(time.time_ns()) ^ int(target_x * 131) ^ int(target_y * 197))
    norm_speed = max(0.25, float(speed))
    dx = target_x - start_x
    dy = target_y - start_y
    dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
    if dist < 2.5:
        _LAST_HUMAN_ROUTE = [(float(start_x), float(start_y)), (float(target_x), float(target_y))]
        try:
            page.evaluate("([x, y]) => window.__bridgeMoveCursor?.(x, y)", [target_x, target_y])
        except Exception:
            pass
        return

    nx = dx / dist
    ny = dy / dist
    perp_x = -ny
    perp_y = nx
    base_amp = max(10.0, min(44.0, dist * rng.uniform(0.1, 0.22)))
    c1x, c1y = _clamp(
        start_x + dx * rng.uniform(0.2, 0.34) + perp_x * base_amp * rng.uniform(-0.9, 0.9),
        start_y + dy * rng.uniform(0.2, 0.34) + perp_y * base_amp * rng.uniform(-0.9, 0.9),
    )
    c2x, c2y = _clamp(
        start_x + dx * rng.uniform(0.58, 0.8) + perp_x * base_amp * rng.uniform(-0.85, 0.85),
        start_y + dy * rng.uniform(0.58, 0.8) + perp_y * base_amp * rng.uniform(-0.85, 0.85),
    )
    overshoot = max(1.5, min(8.0, dist * rng.uniform(0.02, 0.045)))
    ox, oy = _clamp(
        target_x + nx * overshoot + perp_x * rng.uniform(-3.2, 3.2),
        target_y + ny * overshoot + perp_y * rng.uniform(-3.2, 3.2),
    )

    samples = int(max(18, min(52, round((dist / 24.0) + (24.0 / norm_speed)))))
    phase = rng.uniform(0.0, math.pi * 2.0)
    ellipse_cycles = rng.uniform(0.8, 1.8)
    route: list[tuple[float, float]] = []
    for i in range(1, samples + 1):
        t = i / float(samples)
        one_t = 1.0 - t
        bx = (
            one_t * one_t * one_t * start_x
            + 3.0 * one_t * one_t * t * c1x
            + 3.0 * one_t * t * t * c2x
            + t * t * t * ox
        )
        by = (
            one_t * one_t * one_t * start_y
            + 3.0 * one_t * one_t * t * c1y
            + 3.0 * one_t * t * t * c2y
            + t * t * t * oy
        )
        # Elliptical lateral motion with tapering envelope near endpoints.
        env = max(0.0, math.sin(math.pi * t))
        wobble = math.sin((2.0 * math.pi * ellipse_cycles * t) + phase)
        bx += perp_x * (base_amp * 0.84 * env * wobble)
        by += perp_y * (base_amp * 0.84 * env * wobble)
        # Fine-grained noise, stronger at mid-path and softer near target.
        micro = max(0.0, 1.0 - abs(0.52 - t) * 1.85)
        bx += perp_x * rng.uniform(-2.8, 2.8) * micro
        by += perp_y * rng.uniform(-2.8, 2.8) * micro
        route.append(_clamp(bx, by))
    route.append(_clamp(target_x, target_y))
    _LAST_HUMAN_ROUTE = [(float(px), float(py)) for px, py in route]
    route_payload = [[float(px), float(py)] for px, py in route]
    try:
        page.evaluate(
            "pts => { window.__bridgeLastHumanRoute = pts; window.__bridgeDrawPath?.(pts); }",
            route_payload,
        )
    except Exception:
        pass

    last_pause_idx = len(route) - 2
    for idx, (px, py) in enumerate(route):
        if idx < len(route) - 1:
            progress = min(1.0, idx / max(1, len(route) - 1))
            # Decelerate near destination: fewer large jumps, finer final approach.
            slow_factor = progress * progress
            seg_steps = int(
                max(
                    2,
                    min(
                        11,
                        round((3.2 / norm_speed) + (slow_factor * 6.0) + rng.uniform(-0.8, 1.3)),
                    ),
                )
            )
        else:
            seg_steps = 3
        page.mouse.move(px, py, steps=seg_steps)
        try:
            # Feed intermediate points to the visual overlay so the trail reflects the real path.
            page.evaluate("([x, y]) => window.__bridgeMoveCursor?.(x, y)", [px, py])
        except Exception:
            pass
        if idx < last_pause_idx:
            progress = min(1.0, idx / max(1, len(route) - 1))
            # Tiny cadence pauses; slightly longer near end to make slowdown perceptible.
            pause_ms = int(
                max(
                    0,
                    min(
                        18,
                        round((3.2 / norm_speed) + (progress * 5.0) + rng.uniform(-2.0, 2.4)),
                    ),
                )
            )
            if pause_ms > 0:
                try:
                    page.wait_for_timeout(pause_ms)
                except Exception:
                    pass
    try:
        page.evaluate("([x, y]) => window.__bridgeMoveCursor?.(x, y)", [target_x, target_y])
    except Exception:
        pass


def _human_mouse_click(page: Any, x: float, y: float, *, speed: float, hold_ms: int) -> None:
    _human_mouse_move(page, x, y, speed=speed)
    # Tiny jitter right before click to avoid perfectly static pre-click posture.
    try:
        jitter_x = x + random.uniform(-1.5, 1.5)
        jitter_y = y + random.uniform(-1.5, 1.5)
        page.mouse.move(jitter_x, jitter_y, steps=2)
        page.mouse.move(x, y, steps=2)
    except Exception:
        pass
    try:
        page.evaluate("([x, y]) => window.__bridgePulseAt?.(x, y)", [x, y])
    except Exception:
        pass
    page.mouse.down()
    effective_hold = int(max(0, min(260, round(float(hold_ms) * 0.34))))
    if effective_hold > 0:
        page.wait_for_timeout(effective_hold)
    page.mouse.up()
    # Post-click settle: small drift so cursor doesn't remain pinned on exact click coordinate.
    try:
        settle_x = x + random.uniform(-16.0, 16.0)
        settle_y = y + random.uniform(-12.0, 12.0)
        page.mouse.move(settle_x, settle_y, steps=4)
        page.evaluate("([x, y]) => window.__bridgeMoveCursor?.(x, y)", [settle_x, settle_y])
    except Exception:
        pass
