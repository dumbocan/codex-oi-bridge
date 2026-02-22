"""Playwright interaction primitives extracted from the web backend loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from bridge.web_interactive_capture import (
    capture_movement as _capture_movement_artifact,
)
from bridge.web_mouse import (
    _human_mouse_click,
    _human_mouse_move,
    get_last_human_route as _get_last_human_route,
)
from bridge.web_visual_overlay import (
    _highlight_target,
)


def apply_interactive_step(
    *,
    page: Any,
    step: Any,
    step_num: int,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    visual: bool,
    click_pulse_enabled: bool,
    visual_human_mouse: bool,
    visual_mouse_speed: float,
    visual_click_hold_ms: int,
    timeout_ms: int,
    movement_capture_dir: Path | None,
    evidence_paths: list[str] | None,
    disable_active_youtube_iframe_pointer_events: Callable[[Any], dict[str, Any] | None],
    force_main_frame_context: Callable[[Any], bool],
    restore_iframe_pointer_events: Callable[[Any, dict[str, Any] | None], None],
    retry_scroll: Callable[..., None],
    scan_visible_buttons_in_cards: Callable[..., tuple[list[str], bool]],
    scan_visible_selectors: Callable[..., list[str]],
    safe_page_title: Callable[[Any], str],
    is_timeout_error: Callable[[Exception], bool],
    to_repo_rel: Callable[[Path], str],
) -> None:
    iframe_guard = disable_active_youtube_iframe_pointer_events(page)
    if not force_main_frame_context(page):
        restore_iframe_pointer_events(page, iframe_guard)
        raise RuntimeError("Unable to enforce main frame context for interactive step")
    try:
        move_capture_count = 0

        def _capture_movement(tag: str) -> None:
            nonlocal move_capture_count
            move_capture_count = _capture_movement_artifact(
                page=page,
                tag=tag,
                step_num=step_num,
                move_capture_count=move_capture_count,
                visual=visual,
                movement_capture_dir=movement_capture_dir,
                evidence_paths=evidence_paths,
                get_last_human_route=_get_last_human_route,
                to_repo_rel=to_repo_rel,
            )

        if step.kind == "click_selector":
            locator = page.locator(step.target).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            target = _highlight_target(
                page,
                locator,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled and visual,
                show_preview=not (visual and visual_human_mouse),
            )
            if target is None:
                raise SystemExit(f"Target occluded or not visible: selector {step.target}")
            if visual:
                if visual_human_mouse and target:
                    _human_mouse_click(
                        page,
                        target[0],
                        target[1],
                        speed=visual_mouse_speed,
                        hold_ms=visual_click_hold_ms,
                    )
                    _capture_movement("after_click_selector")
                else:
                    locator.click(timeout=timeout_ms)
            else:
                locator.click(timeout=timeout_ms)
            actions.append(f"cmd: playwright click selector:{step.target}")
            observations.append(f"Clicked selector in step {step_num}: {step.target}")
            ui_findings.append(
                f"step {step_num} verify visible result: url={page.url}, title={safe_page_title(page)}"
            )
            return

        if step.kind == "click_text":
            locator = page.locator("body").get_by_text(step.target, exact=False).first
            try:
                locator.wait_for(state="visible", timeout=timeout_ms)
                target = _highlight_target(
                    page,
                    locator,
                    f"step {step_num}",
                    click_pulse_enabled=click_pulse_enabled and visual,
                    show_preview=not (visual and visual_human_mouse),
                )
                if target is None:
                    raise SystemExit(f"Target occluded or not visible: text {step.target}")
                if visual:
                    if visual_human_mouse and target:
                        _human_mouse_click(
                            page,
                            target[0],
                            target[1],
                            speed=visual_mouse_speed,
                            hold_ms=visual_click_hold_ms,
                        )
                        _capture_movement("after_click_text")
                    else:
                        locator.click(timeout=timeout_ms)
                else:
                    locator.click(timeout=timeout_ms)
                actions.append(f"cmd: playwright click text:{step.target}")
                observations.append(f"Clicked text in step {step_num}: {step.target}")
                ui_findings.append(
                    f"step {step_num} verify visible result: url={page.url}, title={safe_page_title(page)}"
                )
                return
            except Exception as exc:
                if is_timeout_error(exc):
                    raise
                raise

        if step.kind == "maybe_click_text":
            locator = page.locator("body").get_by_text(step.target, exact=False).first
            try:
                locator.wait_for(state="visible", timeout=timeout_ms)
                target = _highlight_target(
                    page,
                    locator,
                    f"step {step_num}",
                    click_pulse_enabled=click_pulse_enabled and visual,
                    show_preview=not (visual and visual_human_mouse),
                )
                if target is None:
                    observations.append(f"Step {step_num}: maybe click target not visible/occluded: {step.target}")
                    ui_findings.append(f"step {step_num} verify optional click skipped: {step.target}")
                    return
                if visual and visual_human_mouse and target:
                    _human_mouse_click(
                        page,
                        target[0],
                        target[1],
                        speed=visual_mouse_speed,
                        hold_ms=visual_click_hold_ms,
                    )
                    _capture_movement("after_maybe_click_text")
                else:
                    locator.click(timeout=timeout_ms)
                actions.append(f"cmd: playwright maybe click text:{step.target}")
                observations.append(f"Maybe clicked text in step {step_num}: {step.target}")
                ui_findings.append(
                    f"step {step_num} verify visible result: url={page.url}, title={safe_page_title(page)}"
                )
                return
            except Exception:
                observations.append(f"Step {step_num}: maybe click not present: {step.target}")
                ui_findings.append(f"step {step_num} verify optional click skipped: {step.target}")
                return

        if step.kind == "bulk_click_in_cards":
            card_selector, required_text = ".track-card", ""
            if "||" in step.value:
                left, right = step.value.split("||", 1)
                card_selector = str(left or ".track-card").strip() or ".track-card"
                required_text = str(right or "").strip()
            seen_selectors: set[str] = set()
            clicked = 0
            no_new_rounds = 0
            for _round in range(1, 18):
                selectors, reached_bottom = scan_visible_buttons_in_cards(
                    page,
                    card_selector=card_selector,
                    button_selector=step.target,
                    required_text=required_text,
                    seen=seen_selectors,
                )
                if not selectors:
                    no_new_rounds += 1
                for selector in selectors:
                    locator = page.locator(selector).first
                    try:
                        locator.wait_for(state="visible", timeout=timeout_ms)
                    except Exception:
                        continue
                    target = _highlight_target(
                        page,
                        locator,
                        f"step {step_num} BULK",
                        click_pulse_enabled=click_pulse_enabled and visual,
                        show_preview=not (visual and visual_human_mouse),
                    )
                    if target is None:
                        continue
                    if visual and visual_human_mouse and target:
                        _human_mouse_click(
                            page,
                            target[0],
                            target[1],
                            speed=visual_mouse_speed,
                            hold_ms=visual_click_hold_ms,
                        )
                        _capture_movement("after_bulk_click")
                    else:
                        locator.click(timeout=timeout_ms)
                    seen_selectors.add(selector)
                    clicked += 1
                if no_new_rounds >= 2 and reached_bottom:
                    break
                if no_new_rounds >= 3:
                    break
                if reached_bottom and not selectors:
                    break
                retry_scroll(page, amount=120, pause_ms=160)
            actions.append(
                f"cmd: playwright bulk_click_in_cards selector:{step.target} cards:{card_selector} text:{required_text}"
            )
            observations.append(
                f"Bulk click in cards step {step_num}: selector={step.target}, card={card_selector}, "
                f"text={required_text}, clicked={clicked}"
            )
            ui_findings.append(
                f"step {step_num} verify bulk click in cards: clicked={clicked}, selector={step.target}"
            )
            return

        if step.kind == "bulk_click_until_empty":
            removed = 0
            for _pass in range(1, 24):
                seen: set[str] = set()
                selectors = scan_visible_selectors(page, button_selector=step.target, seen=seen)
                if not selectors:
                    break
                for selector in selectors:
                    locator = page.locator(selector).first
                    try:
                        locator.wait_for(state="visible", timeout=timeout_ms)
                    except Exception:
                        continue
                    target = _highlight_target(
                        page,
                        locator,
                        f"step {step_num} BULK-EMPTY",
                        click_pulse_enabled=click_pulse_enabled and visual,
                        show_preview=not (visual and visual_human_mouse),
                    )
                    if target is None:
                        continue
                    if visual and visual_human_mouse and target:
                        _human_mouse_click(
                            page,
                            target[0],
                            target[1],
                            speed=visual_mouse_speed,
                            hold_ms=visual_click_hold_ms,
                        )
                        _capture_movement("after_bulk_until_empty_click")
                    else:
                        locator.click(timeout=timeout_ms)
                    removed += 1
                    seen.add(selector)
                try:
                    page.wait_for_timeout(110)
                except Exception:
                    pass
            actions.append(f"cmd: playwright bulk_click_until_empty selector:{step.target}")
            observations.append(f"Bulk click until empty step {step_num}: selector={step.target}, clicked={removed}")
            ui_findings.append(
                f"step {step_num} verify bulk click until empty: clicked={removed}, selector={step.target}"
            )
            return

        if step.kind == "select_label":
            locator = page.locator(step.target).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            target = _highlight_target(
                page,
                locator,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled and visual,
                show_preview=not (visual and visual_human_mouse),
            )
            if target is None:
                raise SystemExit(f"Target occluded or not visible: selector {step.target}")
            if visual:
                if visual_human_mouse and target:
                    _human_mouse_move(page, target[0], target[1], speed=visual_mouse_speed)
                    _capture_movement("after_select_label_move")
            locator.select_option(label=step.value)
            actions.append(f"cmd: playwright select selector:{step.target} label:{step.value}")
            observations.append(
                f"Selected option by label in step {step_num}: selector={step.target}, label={step.value}"
            )
            ui_findings.append(
                f"step {step_num} verify visible result: url={page.url}, title={safe_page_title(page)}"
            )
            return

        if step.kind == "fill_selector":
            locator = page.locator(step.target).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            target = _highlight_target(
                page,
                locator,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled and visual,
                show_preview=not (visual and visual_human_mouse),
            )
            if target is None:
                raise SystemExit(f"Target occluded or not visible: selector {step.target}")
            if visual and visual_human_mouse and target:
                _human_mouse_move(page, target[0], target[1], speed=visual_mouse_speed)
                _capture_movement("after_fill_move")
            locator.fill(step.value, timeout=timeout_ms)
            actions.append(f"cmd: playwright fill selector:{step.target} text:{step.value}")
            observations.append(
                f"Filled input in step {step_num}: selector={step.target}, text={step.value}"
            )
            ui_findings.append(
                f"step {step_num} verify visible result: url={page.url}, title={safe_page_title(page)}"
            )
            return

        if step.kind == "select_value":
            locator = page.locator(step.target).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            target = _highlight_target(
                page,
                locator,
                f"step {step_num}",
                click_pulse_enabled=click_pulse_enabled and visual,
                show_preview=not (visual and visual_human_mouse),
            )
            if target is None:
                raise SystemExit(f"Target occluded or not visible: selector {step.target}")
            if visual:
                if visual_human_mouse and target:
                    _human_mouse_move(page, target[0], target[1], speed=visual_mouse_speed)
                    _capture_movement("after_select_value_move")
            locator.select_option(value=step.value)
            actions.append(f"cmd: playwright select selector:{step.target} value:{step.value}")
            observations.append(
                f"Selected option by value in step {step_num}: selector={step.target}, value={step.value}"
            )
            ui_findings.append(
                f"step {step_num} verify visible result: url={page.url}, title={safe_page_title(page)}"
            )
            return
    finally:
        restore_iframe_pointer_events(page, iframe_guard)

    raise RuntimeError(f"Unsupported interactive step kind: {step.kind}")


def apply_wait_step(
    *,
    page: Any,
    step: Any,
    step_num: int,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    timeout_ms: int,
    helpers_apply_wait_step: Callable[..., None],
    disable_active_youtube_iframe_pointer_events: Callable[[Any], dict[str, Any] | None],
    force_main_frame_context: Callable[[Any], bool],
    restore_iframe_pointer_events: Callable[[Any, dict[str, Any] | None], None],
) -> None:
    helpers_apply_wait_step(
        page,
        step,
        step_num,
        actions,
        observations,
        ui_findings,
        timeout_ms=timeout_ms,
        disable_active_youtube_iframe_pointer_events=disable_active_youtube_iframe_pointer_events,
        force_main_frame_context=force_main_frame_context,
        restore_iframe_pointer_events=restore_iframe_pointer_events,
    )
