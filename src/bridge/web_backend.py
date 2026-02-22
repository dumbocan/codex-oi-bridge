"""Deterministic web interaction backend using Playwright."""

from __future__ import annotations

import json
import re
import socket
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from bridge.models import OIReport
from bridge.web_common import (
    collapse_ws as _collapse_ws,
    is_generic_play_label as _is_generic_play_label,
    is_valid_url as _is_valid_url,
    normalize_url as _normalize_url,
    playwright_available as _playwright_available,
    safe_page_title as _safe_page_title,
    same_origin_path as _same_origin_path,
)
from bridge.web_overlay import (
    destroy_top_bar as _destroy_top_bar,
    notify_learning_state as _notify_learning_state,
    session_state_payload as _session_state_payload,
    set_assistant_control_overlay as _set_assistant_control_overlay,
    set_learning_handoff_overlay as _set_learning_handoff_overlay,
    set_user_control_overlay as _set_user_control_overlay,
    update_top_bar_state as _update_top_bar_state,
)
from bridge.web_bulk_scan import (
    scan_visible_buttons_in_cards as _bulk_scan_visible_buttons_in_cards,
    scan_visible_selectors as _bulk_scan_visible_selectors,
)
from bridge.web_executor_steps import (
    INTERACTIVE_STEP_KINDS,
    append_iframe_focus_findings,
    append_interactive_timeout_findings,
    append_run_crash_findings,
    append_wait_timeout_findings,
    step_learning_target as _step_learning_target,
)
from bridge.web_frame_guard import (
    disable_active_youtube_iframe_pointer_events as _frame_disable_active_youtube_iframe_pointer_events,
    force_main_frame_context as _frame_force_main_frame_context,
    is_iframe_focus_locked as _frame_is_iframe_focus_locked,
    restore_iframe_pointer_events as _frame_restore_iframe_pointer_events,
)
from bridge.web_handoff import (
    show_custom_handoff_notice as _handoff_show_custom_notice,
    show_stuck_handoff_notice as _handoff_show_stuck_notice,
    trigger_stuck_handoff as _handoff_trigger_stuck,
)
from bridge.web_interaction_helpers import (
    apply_wait_step as _helpers_apply_wait_step,
    retry_scroll as _retry_scroll,
    semantic_hints_for_selector as _semantic_hints_for_selector,
    stable_selectors_for_target as _stable_selectors_for_target,
)
from bridge.web_interaction_executor import (
    apply_interactive_step as _executor_apply_interactive_step,
    apply_wait_step as _executor_apply_wait_step,
)
from bridge.web_interactive_retries import (
    RetryResult as _RetryResult,
    apply_interactive_step_with_retries as _retries_apply_interactive_step_with_retries,
)
from bridge.web_learning_store import (
    is_learning_target_candidate as _learning_is_target_candidate,
    is_specific_selector as _learning_is_specific_selector,
    learned_selectors_for_step as _learning_learned_selectors_for_step,
    load_learned_selectors as _learning_load_learned_selectors,
    normalize_learning_target_key as _learning_normalize_target_key,
    prioritize_steps_with_learned_selectors as _learning_prioritize_steps,
    store_learned_selector as _learning_store_learned_selector,
)
from bridge.web_run_finalize import (
    finalize_result as _finalize_result,
)
from bridge.web_run_handoff import (
    HandoffDecision,
    evaluate_iframe_precheck_handoff as _evaluate_iframe_precheck_handoff,
    evaluate_timeout_handoff as _evaluate_timeout_handoff,
    evaluate_watchdog_handoff as _evaluate_watchdog_handoff,
)
from bridge.web_run_state import (
    WebRunMutableState,
    apply_handoff_decision as _state_apply_handoff_decision,
    apply_handoff_updates as _state_apply_handoff_updates,
)
from bridge.web_preflight import (
    execute_preflight as _execute_preflight,
)
from bridge.web_run_bootstrap import (
    apply_runtime_page_timeout as _bootstrap_apply_runtime_page_timeout,
    attach_page_observers as _bootstrap_attach_page_observers,
    install_visual_overlay_initial as _bootstrap_install_visual_overlay_initial,
    load_run_timing_config as _bootstrap_load_run_timing_config,
    setup_browser_page as _bootstrap_setup_browser_page,
)
from bridge.web_run_postloop import (
    cleanup_after_run as _postloop_cleanup_after_run,
    process_post_loop_handoff_and_learning as _postloop_process_handoff_and_learning,
)
from bridge.web_run_loop import (
    execute_steps_loop as _loop_execute_steps_loop,
)
from bridge.web_run_reporting import (
    build_web_report as _reporting_build_web_report,
    persist_report_and_status as _reporting_persist_report_and_status,
)
from bridge.storage import write_json, write_status
from bridge.web_teaching import (
    capture_manual_learning as _teaching_capture_manual_learning,
    is_relevant_manual_learning_event as _teaching_is_relevant_manual_learning_event,
    normalize_failed_target_label as _teaching_normalize_failed_target_label,
    process_learning_window as _teaching_process_learning_window,
    release_control_for_handoff as _teaching_release_control_for_handoff,
    resume_after_learning as _teaching_resume_after_learning,
    show_learning_thanks_notice as _teaching_show_learning_thanks_notice,
    show_teaching_handoff_notice as _teaching_show_handoff_notice,
    show_wrong_manual_click_notice as _teaching_show_wrong_click_notice,
    write_teaching_artifacts as _teaching_write_artifacts,
)
from bridge.web_watchdog import (
    WebWatchdogState,
    remaining_ms as watchdog_remaining_ms,
    update_step_signature,
)
from bridge.web_handoff_actions import (
    retry_stuck_handoff as _retry_stuck_handoff,
    target_not_found_handoff as _target_not_found_handoff,
)
from bridge.web_visual_overlay import (
    _highlight_target as _visual_highlight_target,
    _install_visual_overlay,
)
from bridge.web_steps import (
    WebStep,
    parse_steps,
)
from bridge.web_step_runner import (
    apply_step_common_prechecks as _step_apply_common_prechecks,
    append_skipped_not_applicable as _step_append_skipped_not_applicable,
    execute_interactive_step as _step_execute_interactive_step,
    execute_wait_step as _step_execute_wait_step,
    record_step_outcome as _step_record_step_outcome,
)
from bridge.web_step_applicability import (
    interactive_step_not_applicable_reason as _step_interactive_not_applicable_reason,
    is_timeout_error as _step_is_timeout_error,
    probe_step_target_state as _step_probe_target_state,
)
from bridge.web_visual_runtime import (
    ensure_visual_overlay_ready as _visual_ensure_overlay_ready,
    ensure_visual_overlay_ready_best_effort as _visual_ensure_overlay_ready_best_effort,
    force_visual_overlay_reinstall as _visual_force_overlay_reinstall,
)
from bridge.web_runtime_safety import (
    capture_timeout_evidence as _safety_capture_timeout_evidence,
    is_page_closed_error as _safety_is_page_closed_error,
    page_is_closed as _safety_page_is_closed,
    runtime_closed as _safety_runtime_closed,
    to_repo_rel as _safety_to_repo_rel,
)
from bridge.web_session import (
    WebSession,
    mark_controlled,
    request_session_state,
)
from bridge.web_session_overlay_ops import (
    destroy_session_top_bar as _ops_destroy_session_top_bar,
    ensure_session_top_bar as _ops_ensure_session_top_bar,
    release_session_control_overlay as _ops_release_session_control_overlay,
)
from bridge.web_target_preflight import (
    http_quick_check as _preflight_http_quick_check_impl,
    preflight_stack_prereqs as _preflight_stack_prereqs_impl,
    preflight_target_reachable as _preflight_target_reachable_impl,
)

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")

_LEARNING_DIR = Path("runs") / "learning"
_LEARNING_JSON = _LEARNING_DIR / "web_teaching_selectors.json"


def _observer_noise_mode() -> str:
    raw = str(os.getenv("BRIDGE_OBSERVER_NOISE_MODE", "minimal")).strip().lower()
    return "debug" if raw == "debug" else "minimal"


def run_web_task(
    task: str,
    run_dir: Path,
    timeout_seconds: int,
    verified: bool = False,
    progress_cb: Callable[[int, int, str], None] | None = None,
    visual: bool = False,
    visual_cursor: bool = True,
    visual_click_pulse: bool = True,
    visual_scale: float = 1.0,
    visual_color: str = "#3BA7FF",
    visual_human_mouse: bool = True,
    visual_mouse_speed: float = 1.0,
    visual_click_hold_ms: int = 180,
    session: WebSession | None = None,
    keep_open: bool = False,
    teaching_mode: bool = False,
) -> OIReport:
    url_match = _URL_RE.search(task)
    if not url_match:
        raise SystemExit("Web mode requires an explicit URL in task.")
    url = _normalize_url(url_match.group(0))
    if not _is_valid_url(url):
        raise SystemExit(f"Web mode received invalid URL token: {url_match.group(0)}")
    _preflight_target_reachable(url)
    _preflight_stack_prereqs()
    steps = _parse_steps(task)

    if not _playwright_available():
        raise SystemExit(
            "Playwright Python package is not installed. "
            "Install it in the environment to use --mode web."
        )

    report: OIReport | None = None
    try:
        report = _execute_playwright(
            url,
            steps,
            run_dir,
            timeout_seconds,
            verified=verified,
            progress_cb=progress_cb,
            visual=visual,
            visual_cursor=visual_cursor,
            visual_click_pulse=visual_click_pulse,
            visual_scale=visual_scale,
            visual_color=visual_color,
            visual_human_mouse=visual_human_mouse,
            visual_mouse_speed=visual_mouse_speed,
            visual_click_hold_ms=visual_click_hold_ms,
            session=session,
            keep_open=keep_open,
            teaching_mode=teaching_mode,
        )
    except BaseException as exc:
        msg = str(exc) or exc.__class__.__name__
        report = OIReport(
            task_id=run_dir.name,
            goal=f"web: {url}",
            actions=[],
            observations=["web executor aborted before completion"],
            console_errors=[f"Unhandled web execution error: {msg}"],
            network_findings=[],
            ui_findings=[
                "what_failed=run_crash",
                "where=web-run",
                f"why_likely={msg}",
                "attempted=executor run",
                "next_best_action=inspect logs and retry",
                "final_state=failed",
            ],
            result="failed",
            evidence_paths=[],
        )
    finally:
        _reporting_persist_report_and_status(
            report=report,
            run_dir=run_dir,
            task=task,
            write_json_fn=write_json,
            write_status_fn=write_status,
        )
    return report


def _preflight_target_reachable(url: str, timeout_seconds: float = 1.2) -> None:
    _preflight_target_reachable_impl(
        url,
        timeout_seconds=timeout_seconds,
        create_connection_fn=socket.create_connection,
    )


def _http_quick_check(url: str, timeout_seconds: float = 1.2) -> None:
    _preflight_http_quick_check_impl(url, timeout_seconds=timeout_seconds)


def _preflight_stack_prereqs() -> None:
    _preflight_stack_prereqs_impl(http_quick_check_fn=_http_quick_check)


def release_session_control_overlay(session: WebSession) -> None:
    _ops_release_session_control_overlay(
        session,
        set_assistant_control_overlay=_set_assistant_control_overlay,
        update_top_bar_state=_update_top_bar_state,
        session_state_payload=_session_state_payload,
    )


def destroy_session_top_bar(session: WebSession) -> None:
    _ops_destroy_session_top_bar(session, destroy_top_bar=_destroy_top_bar)


def ensure_session_top_bar(session: WebSession) -> None:
    _ops_ensure_session_top_bar(
        session,
        install_visual_overlay=_install_visual_overlay,
        set_assistant_control_overlay=_set_assistant_control_overlay,
        update_top_bar_state=_update_top_bar_state,
        session_state_payload=_session_state_payload,
    )


def _parse_steps(task: str) -> list[WebStep]:
    return parse_steps(task)


def _page_is_closed(page: Any | None) -> bool:
    return _safety_page_is_closed(page)


def _is_page_closed_error(exc: BaseException) -> bool:
    return _safety_is_page_closed_error(exc)


def _runtime_closed(page: Any | None, session: WebSession | None) -> bool:
    return _safety_runtime_closed(page, session)


def _execute_playwright(
    url: str,
    steps: list[WebStep],
    run_dir: Path,
    timeout_seconds: int,
    *,
    verified: bool,
    progress_cb: Callable[[int, int, str], None] | None = None,
    visual: bool = False,
    visual_cursor: bool = True,
    visual_click_pulse: bool = True,
    visual_scale: float = 1.0,
    visual_color: str = "#3BA7FF",
    visual_human_mouse: bool = True,
    visual_mouse_speed: float = 1.0,
    visual_click_hold_ms: int = 180,
    session: WebSession | None = None,
    keep_open: bool = False,
    teaching_mode: bool = False,
) -> OIReport:
    from playwright.sync_api import sync_playwright

    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    actions: list[str] = []
    if visual:
        actions.append("cmd: playwright visual on")
    observations: list[str] = []
    console_errors: list[str] = []
    network_findings: list[str] = []
    ui_findings: list[str] = []
    evidence_paths: list[str] = []
    learning_notes: list[str] = []
    learning_context: dict[str, str] = {}
    run_state = WebRunMutableState()
    page = None
    interactive_timeout_ms = 8000
    learned_selector_map = _load_learned_selectors()
    watchdog_state = WebWatchdogState()
    timing_cfg = _bootstrap_load_run_timing_config()
    step_hard_timeout_seconds = timing_cfg.step_hard_timeout_seconds
    run_hard_timeout_seconds = timing_cfg.run_hard_timeout_seconds
    run_deadline_ts = timing_cfg.run_deadline_ts

    with sync_playwright() as p:
        setup = _bootstrap_setup_browser_page(
            playwright_obj=p,
            session=session,
            url=url,
            visual=visual,
            visual_mouse_speed=visual_mouse_speed,
            timeout_seconds=timeout_seconds,
            launch_browser=_launch_browser,
            mark_controlled=mark_controlled,
            safe_page_title=_safe_page_title,
        )
        browser = setup.browser
        page = setup.page
        attached = setup.attached
        overlay_debug_path = evidence_dir / "step_overlay_debug.png"
        _bootstrap_install_visual_overlay_initial(
            page=page,
            visual=visual,
            attached=attached,
            visual_cursor=visual_cursor,
            visual_click_pulse=visual_click_pulse,
            visual_scale=visual_scale,
            visual_color=visual_color,
            session=session,
            ui_findings=ui_findings,
            overlay_debug_path=overlay_debug_path,
            install_visual_overlay=_install_visual_overlay,
            session_state_payload=_session_state_payload,
            ensure_visual_overlay_ready_best_effort=_ensure_visual_overlay_ready_best_effort,
        )
        _bootstrap_attach_page_observers(
            page=page,
            console_errors=console_errors,
            network_findings=network_findings,
        )

        run_state.control_enabled = False
        wait_timeout_ms = timing_cfg.wait_timeout_ms
        interactive_timeout_ms = timing_cfg.interactive_timeout_ms
        learning_window_seconds = timing_cfg.learning_window_seconds
        post_action_pause_ms = timing_cfg.post_action_pause_ms
        watchdog_cfg = timing_cfg.watchdog_cfg
        _bootstrap_apply_runtime_page_timeout(
            page=page,
            timeout_seconds=timeout_seconds,
            run_hard_timeout_seconds=run_hard_timeout_seconds,
        )
        watchdog_state.last_useful_events = _observer_useful_event_count(session)
        watchdog_state.last_step_change_ts = time.monotonic()
        watchdog_state.last_progress_event_ts = watchdog_state.last_step_change_ts
        try:
            preflight = _execute_preflight(
                page=page,
                url=url,
                visual=visual,
                visual_cursor=visual_cursor,
                overlay_debug_path=overlay_debug_path,
                evidence_dir=evidence_dir,
                actions=actions,
                observations=observations,
                ui_findings=ui_findings,
                evidence_paths=evidence_paths,
                attached=attached,
                session=session,
                control_enabled=run_state.control_enabled,
                learning_context_fn=_learning_context,
                safe_page_title=_safe_page_title,
                same_origin_path=_same_origin_path,
                ensure_visual_overlay_ready=_ensure_visual_overlay_ready_best_effort,
                set_assistant_control_overlay=_set_assistant_control_overlay,
                update_top_bar_state=_update_top_bar_state,
                session_state_payload=_session_state_payload,
                mark_controlled=mark_controlled,
                to_repo_rel=_to_repo_rel,
                collapse_ws=_collapse_ws,
            )
            learning_context = preflight.learning_context
            run_state.control_enabled = preflight.control_enabled

            def _remaining_ms(deadline_ts: float) -> int:
                return watchdog_remaining_ms(deadline_ts, now_ts=time.monotonic())

            def _apply_handoff_decision(decision: HandoffDecision) -> bool:
                return _state_apply_handoff_decision(run_state, decision)

            def _watchdog_stuck_attempt(attempted: str) -> bool:
                decision = _evaluate_watchdog_handoff(
                    page=page,
                    session=session,
                    watchdog_state=watchdog_state,
                    watchdog_cfg=watchdog_cfg,
                    attempted=attempted,
                    teaching_mode=teaching_mode,
                    visual=visual,
                    control_enabled=run_state.control_enabled,
                    learning_window_seconds=learning_window_seconds,
                    ui_findings=ui_findings,
                    actions=actions,
                    observer_useful_event_count=_observer_useful_event_count,
                    is_iframe_focus_locked=_is_iframe_focus_locked,
                    show_custom_notice=_show_custom_handoff_notice,
                    trigger_stuck_handoff=_trigger_stuck_handoff,
                )
                return _apply_handoff_decision(decision)

            def _trigger_timeout_handoff(
                *,
                what_failed: str,
                where: str,
                learning_target: str,
                attempted: str,
                why_likely: str,
                notice_message: str,
            ) -> bool:
                decision = _evaluate_timeout_handoff(
                    page=page,
                    session=session,
                    what_failed=what_failed,
                    where=where,
                    learning_target=learning_target,
                    attempted=attempted,
                    why_likely=why_likely,
                    notice_message=notice_message,
                    teaching_mode=teaching_mode,
                    visual=visual,
                    control_enabled=run_state.control_enabled,
                    learning_window_seconds=learning_window_seconds,
                    ui_findings=ui_findings,
                    actions=actions,
                    is_learning_target_candidate=_is_learning_target_candidate,
                    trigger_stuck_handoff=_trigger_stuck_handoff,
                )
                return _apply_handoff_decision(decision)

            def _apply_handoff_updates(updates: dict[str, Any]) -> bool:
                return _state_apply_handoff_updates(run_state, updates)

            loop_result = _loop_execute_steps_loop(
                page=page,
                steps=steps,
                session=session,
                run_state=run_state,
                watchdog_state=watchdog_state,
                run_deadline_ts=run_deadline_ts,
                step_hard_timeout_seconds=step_hard_timeout_seconds,
                interactive_timeout_ms=interactive_timeout_ms,
                wait_timeout_ms=wait_timeout_ms,
                learning_window_seconds=learning_window_seconds,
                post_action_pause_ms=post_action_pause_ms,
                visual=visual,
                visual_cursor=visual_cursor,
                visual_click_pulse=visual_click_pulse,
                visual_human_mouse=visual_human_mouse,
                visual_mouse_speed=visual_mouse_speed,
                visual_click_hold_ms=visual_click_hold_ms,
                teaching_mode=teaching_mode,
                progress_cb=progress_cb,
                overlay_debug_path=overlay_debug_path,
                evidence_dir=evidence_dir,
                learned_selector_map=learned_selector_map,
                learning_context=learning_context,
                actions=actions,
                observations=observations,
                ui_findings=ui_findings,
                console_errors=console_errors,
                evidence_paths=evidence_paths,
                learning_notes=learning_notes,
                stuck_interactive_seconds=watchdog_cfg.stuck_interactive_seconds,
                stuck_step_seconds=watchdog_cfg.stuck_step_seconds,
                interactive_step_kinds=set(INTERACTIVE_STEP_KINDS),
                step_learning_target=_step_learning_target,
                update_step_signature=update_step_signature,
                apply_step_common_prechecks=_step_apply_common_prechecks,
                interactive_step_not_applicable_reason=_interactive_step_not_applicable_reason,
                append_skipped_not_applicable=_step_append_skipped_not_applicable,
                record_step_outcome=_step_record_step_outcome,
                execute_interactive_step=_step_execute_interactive_step,
                execute_wait_step=_step_execute_wait_step,
                evaluate_iframe_precheck_handoff=_evaluate_iframe_precheck_handoff,
                show_custom_handoff_notice=_show_custom_handoff_notice,
                append_iframe_focus_findings=append_iframe_focus_findings,
                capture_timeout_evidence=_capture_timeout_evidence,
                apply_interactive_step_with_retries=_apply_interactive_step_with_retries,
                apply_interactive_step=_apply_interactive_step,
                learned_selectors_for_step=_learned_selectors_for_step,
                retry_stuck_handoff=_retry_stuck_handoff,
                target_not_found_handoff=_target_not_found_handoff,
                should_soft_skip_wait_timeout=_should_soft_skip_wait_timeout,
                apply_wait_step=_apply_wait_step,
                append_run_crash_findings=append_run_crash_findings,
                append_interactive_timeout_findings=append_interactive_timeout_findings,
                append_wait_timeout_findings=append_wait_timeout_findings,
                ensure_visual_overlay_ready_best_effort=_ensure_visual_overlay_ready_best_effort,
                remaining_ms=_remaining_ms,
                trigger_timeout_handoff=_trigger_timeout_handoff,
                watchdog_stuck_attempt=_watchdog_stuck_attempt,
                apply_handoff_decision=_apply_handoff_decision,
                apply_handoff_updates=_apply_handoff_updates,
                force_main_frame_context=_force_main_frame_context,
                runtime_closed=_runtime_closed,
                is_page_closed_error=_is_page_closed_error,
                is_timeout_error=_is_timeout_error,
                trigger_stuck_handoff=_trigger_stuck_handoff,
                show_teaching_notice=_show_teaching_handoff_notice,
                store_learned_selector=_store_learned_selector,
            )
            ui_findings.append(f"steps_outcome={json.dumps(loop_result.step_outcomes, ensure_ascii=False)}")
            _postloop_process_handoff_and_learning(
                page=page,
                session=session,
                visual=visual,
                run_state=run_state,
                learning_context=learning_context,
                learning_window_seconds=learning_window_seconds,
                run_dir=run_dir,
                actions=actions,
                observations=observations,
                ui_findings=ui_findings,
                evidence_paths=evidence_paths,
                teaching_release_control_for_handoff=_teaching_release_control_for_handoff,
                teaching_process_learning_window=_teaching_process_learning_window,
                capture_manual_learning=_capture_manual_learning,
                stable_selectors_for_target=_stable_selectors_for_target,
                store_learned_selector=_store_learned_selector,
                write_teaching_artifacts=_write_teaching_artifacts,
                show_learning_thanks_notice=_show_learning_thanks_notice,
                resume_after_learning=_resume_after_learning,
                notify_learning_state=_notify_learning_state,
                update_top_bar_state=_update_top_bar_state,
                session_state_payload=_session_state_payload,
                disable_active_youtube_iframe_pointer_events=_disable_active_youtube_iframe_pointer_events,
                restore_iframe_pointer_events=_restore_iframe_pointer_events,
                mark_controlled=mark_controlled,
                safe_page_title=_safe_page_title,
                set_assistant_control_overlay=_set_assistant_control_overlay,
                set_learning_handoff_overlay=_set_learning_handoff_overlay,
                set_user_control_overlay=_set_user_control_overlay,
            )
        finally:
            _postloop_cleanup_after_run(
                page=page,
                browser=browser,
                session=session,
                attached=attached,
                visual=visual,
                keep_open=keep_open,
                run_state=run_state,
                ui_findings=ui_findings,
                set_learning_handoff_overlay=_set_learning_handoff_overlay,
                set_assistant_control_overlay=_set_assistant_control_overlay,
                update_top_bar_state=_update_top_bar_state,
                session_state_payload=_session_state_payload,
                mark_controlled=mark_controlled,
                safe_page_title=_safe_page_title,
            )

    result = run_state.result or "success"
    result = _finalize_result(
        result=result,
        force_keep_open=run_state.force_keep_open,
        console_errors=console_errors,
        network_findings=network_findings,
        verified=verified,
        steps_count=len(steps),
        ui_findings=ui_findings,
        where_default=watchdog_state.current_step_signature or "web-run",
    )

    return _reporting_build_web_report(
        run_id=run_dir.name,
        url=url,
        actions=actions,
        observations=observations,
        console_errors=console_errors,
        network_findings=network_findings,
        ui_findings=ui_findings,
        result=result,
        evidence_paths=evidence_paths,
    )


def _observer_useful_event_count(session: WebSession | None) -> int:
    if session is None:
        return 0
    try:
        state = request_session_state(session)
    except BaseException:
        return 0
    events = list(state.get("recent_events", []) or [])
    noise_mode = str(state.get("observer_noise_mode", _observer_noise_mode())).strip().lower()
    useful_types = {"click", "network_warn", "network_error", "console_error", "page_error"}
    if noise_mode == "debug":
        useful_types.update({"scroll", "mousemove"})
    count = 0
    for evt in events:
        if not isinstance(evt, dict):
            continue
        etype = str(evt.get("type", "")).strip().lower()
        if etype in useful_types:
            count += 1
    return count


def _capture_timeout_evidence(
    *,
    page: Any,
    evidence_dir: Path,
    evidence_paths: list[str],
    name: str,
) -> None:
    _safety_capture_timeout_evidence(
        page=page,
        evidence_dir=evidence_dir,
        evidence_paths=evidence_paths,
        name=name,
    )


def _apply_interactive_step_with_retries(
    page: Any,
    step: WebStep,
    step_num: int,
    evidence_dir: Path,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    evidence_paths: list[str],
    *,
    visual: bool,
    click_pulse_enabled: bool,
    visual_human_mouse: bool,
    visual_mouse_speed: float,
    visual_click_hold_ms: int,
    timeout_ms: int,
    max_retries: int,
    learning_selectors: list[str],
    session: WebSession | None,
    step_label: str,
    stuck_interactive_seconds: float,
    stuck_step_seconds: float,
    step_deadline_ts: float,
    run_deadline_ts: float,
) -> _RetryResult:
    return _retries_apply_interactive_step_with_retries(
        page=page,
        step=step,
        step_num=step_num,
        evidence_dir=evidence_dir,
        actions=actions,
        observations=observations,
        ui_findings=ui_findings,
        evidence_paths=evidence_paths,
        visual=visual,
        click_pulse_enabled=click_pulse_enabled,
        visual_human_mouse=visual_human_mouse,
        visual_mouse_speed=visual_mouse_speed,
        visual_click_hold_ms=visual_click_hold_ms,
        timeout_ms=timeout_ms,
        max_retries=max_retries,
        learning_selectors=learning_selectors,
        session=session,
        step_label=step_label,
        stuck_interactive_seconds=stuck_interactive_seconds,
        stuck_step_seconds=stuck_step_seconds,
        step_deadline_ts=step_deadline_ts,
        run_deadline_ts=run_deadline_ts,
        to_repo_rel=_to_repo_rel,
        observer_useful_event_count=_observer_useful_event_count,
        retry_scroll=_retry_scroll,
        apply_interactive_step=_apply_interactive_step,
        is_generic_play_label=_is_generic_play_label,
        stable_selectors_for_target=_stable_selectors_for_target,
        is_specific_selector=_is_specific_selector,
        semantic_hints_for_selector=_semantic_hints_for_selector,
    )


def _learning_context(url: str, title: str) -> dict[str, str]:
    parsed = urlparse(str(url))
    hostname = str(parsed.netloc or "").lower()
    path = str(parsed.path or "/")
    title_norm = _collapse_ws(title).lower()[:80]
    return {
        "hostname": hostname,
        "path": path,
        "title_hint": title_norm,
        "state_key": f"{hostname}{path}|{title_norm}",
    }


def _load_learned_selectors() -> dict[str, dict[str, list[str]]]:
    return _learning_load_learned_selectors(_LEARNING_JSON)


def _store_learned_selector(
    *,
    target: str,
    selector: str,
    context: dict[str, str],
    source: str,
) -> None:
    _learning_store_learned_selector(
        learning_dir=_LEARNING_DIR,
        learning_json=_LEARNING_JSON,
        target=target,
        selector=selector,
        context=context,
        source=source,
        normalize_failed_target_label=_normalize_failed_target_label,
    )


def _write_learning_audit(target: str, selector: str, context: dict[str, str], source: str) -> None:
    from bridge.web_learning_store import write_learning_audit as _learning_write_learning_audit

    _learning_write_learning_audit(
        learning_dir=_LEARNING_DIR,
        target=target,
        selector=selector,
        context=context,
        source=source,
    )


def _normalize_learning_target_key(raw: str, *, selector: str = "") -> str:
    return _learning_normalize_target_key(
        raw,
        selector=selector,
        normalize_failed_target_label=_normalize_failed_target_label,
    )


def _is_learning_target_candidate(target: str) -> bool:
    return _learning_is_target_candidate(target)


def _is_specific_selector(selector: str) -> bool:
    return _learning_is_specific_selector(selector)


def _learned_selectors_for_step(
    step: WebStep,
    selector_map: dict[str, dict[str, list[str]]],
    context: dict[str, str],
) -> list[str]:
    return _learning_learned_selectors_for_step(
        step=step,
        selector_map=selector_map,
        context=context,
        normalize_failed_target_label=_normalize_failed_target_label,
    )


def _should_soft_skip_wait_timeout(
    *, steps: list[WebStep], idx: int, step: WebStep, teaching_mode: bool
) -> bool:
    if not teaching_mode or step.kind != "wait_text":
        return False
    remaining = steps[idx:]
    return any(candidate.kind in INTERACTIVE_STEP_KINDS for candidate in remaining)


def _prioritize_steps_with_learned_selectors(
    steps: list[WebStep],
    selector_map: dict[str, dict[str, list[str]]],
    context: dict[str, str],
) -> list[WebStep]:
    return _learning_prioritize_steps(
        steps=steps,
        selector_map=selector_map,
        context=context,
        normalize_failed_target_label=_normalize_failed_target_label,
        step_factory=WebStep,
    )


def _show_teaching_handoff_notice(page: Any, target: str) -> None:
    _teaching_show_handoff_notice(page, target)


def _show_stuck_handoff_notice(page: Any, step_text: str) -> None:
    _handoff_show_stuck_notice(page, step_text)


def _show_custom_handoff_notice(page: Any, message: str) -> None:
    _handoff_show_custom_notice(page, message)


def _trigger_stuck_handoff(
    *,
    page: Any,
    session: WebSession | None,
    visual: bool,
    control_enabled: bool,
    where: str,
    attempted: str,
    learning_window_seconds: int,
    actions: list[str],
    ui_findings: list[str],
    what_failed: str = "stuck",
    notice_message: str = "",
    why_likely: str = "step unchanged/no useful progress within stuck thresholds during teaching mode",
) -> bool:
    return _handoff_trigger_stuck(
        page=page,
        session=session,
        visual=visual,
        control_enabled=control_enabled,
        where=where,
        attempted=attempted,
        learning_window_seconds=learning_window_seconds,
        actions=actions,
        ui_findings=ui_findings,
        what_failed=what_failed,
        notice_message=notice_message,
        why_likely=why_likely,
        show_custom_notice=_show_custom_handoff_notice,
        show_stuck_notice=_show_stuck_handoff_notice,
        set_learning_handoff_overlay=_set_learning_handoff_overlay,
        set_assistant_control_overlay=_set_assistant_control_overlay,
        mark_controlled=mark_controlled,
        safe_page_title=_safe_page_title,
        notify_learning_state=_notify_learning_state,
        update_top_bar_state=_update_top_bar_state,
        session_state_payload=_session_state_payload,
    )


def _is_iframe_focus_locked(page: Any) -> bool:
    return _frame_is_iframe_focus_locked(page)


def _disable_active_youtube_iframe_pointer_events(page: Any) -> dict[str, Any] | None:
    return _frame_disable_active_youtube_iframe_pointer_events(
        page,
        page_is_closed=_page_is_closed,
    )


def _restore_iframe_pointer_events(page: Any, token: dict[str, Any] | None) -> None:
    _frame_restore_iframe_pointer_events(
        page,
        token,
        page_is_closed=_page_is_closed,
    )


def _force_main_frame_context(page: Any, max_seconds: float = 8.0) -> bool:
    return _frame_force_main_frame_context(
        page,
        max_seconds=max_seconds,
        iframe_focus_locked=_is_iframe_focus_locked,
    )


def _show_learning_thanks_notice(page: Any, target: str) -> None:
    _teaching_show_learning_thanks_notice(page, target)


def _normalize_failed_target_label(raw: str) -> str:
    return _teaching_normalize_failed_target_label(raw)


def _show_wrong_manual_click_notice(page: Any, failed_target: str) -> None:
    _teaching_show_wrong_click_notice(page, failed_target, _stable_selectors_for_target)


def _capture_manual_learning(
    *,
    page: Any | None,
    session: WebSession,
    failed_target: str,
    context: dict[str, str],
    wait_seconds: int,
) -> dict[str, Any] | None:
    return _teaching_capture_manual_learning(
        page=page,
        session=session,
        failed_target=failed_target,
        context=context,
        wait_seconds=wait_seconds,
        request_session_state=request_session_state,
        show_wrong_click_notice=_show_wrong_manual_click_notice,
    )


def _is_relevant_manual_learning_event(evt: dict[str, Any], failed_target: str) -> bool:
    return _teaching_is_relevant_manual_learning_event(evt, failed_target)


def _write_teaching_artifacts(run_dir: Path, payload: dict[str, Any]) -> list[str]:
    return _teaching_write_artifacts(run_dir, payload, _to_repo_rel)


def _resume_after_learning(
    *,
    page: Any,
    selector: str,
    target: str,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
) -> bool:
    return _teaching_resume_after_learning(
        page=page,
        selector=selector,
        target=target,
        actions=actions,
        observations=observations,
        ui_findings=ui_findings,
    )


def _scan_visible_buttons_in_cards(
    page: Any,
    *,
    card_selector: str,
    button_selector: str,
    required_text: str,
    seen: set[str],
) -> tuple[list[str], bool]:
    return _bulk_scan_visible_buttons_in_cards(
        page,
        card_selector=card_selector,
        button_selector=button_selector,
        required_text=required_text,
        seen=seen,
    )


def _scan_visible_selectors(page: Any, *, button_selector: str, seen: set[str]) -> list[str]:
    return _bulk_scan_visible_selectors(page, button_selector=button_selector, seen=seen)


def _highlight_target(*args: Any, **kwargs: Any) -> Any:
    page = args[0] if args else kwargs.get("page")
    locator = args[1] if len(args) > 1 else kwargs.get("locator")
    for attempt in range(3):
        pt = _visual_highlight_target(*args, **kwargs)
        if pt is not None:
            return pt
        if page is None or locator is None:
            return None
        try:
            locator.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            page.evaluate("() => window.scrollBy(0, -220)")
        except Exception:
            pass
        try:
            page.wait_for_timeout(80)
        except Exception:
            pass
    return None


def _apply_interactive_step(
    page: Any,
    step: WebStep,
    step_num: int,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    *,
    visual: bool = False,
    click_pulse_enabled: bool = True,
    visual_human_mouse: bool = True,
    visual_mouse_speed: float = 1.0,
    visual_click_hold_ms: int = 180,
    timeout_ms: int = 8000,
    movement_capture_dir: Path | None = None,
    evidence_paths: list[str] | None = None,
) -> None:
    _executor_apply_interactive_step(
        page=page,
        step=step,
        step_num=step_num,
        actions=actions,
        observations=observations,
        ui_findings=ui_findings,
        visual=visual,
        click_pulse_enabled=click_pulse_enabled,
        visual_human_mouse=visual_human_mouse,
        visual_mouse_speed=visual_mouse_speed,
        visual_click_hold_ms=visual_click_hold_ms,
        timeout_ms=timeout_ms,
        movement_capture_dir=movement_capture_dir,
        evidence_paths=evidence_paths,
        disable_active_youtube_iframe_pointer_events=_disable_active_youtube_iframe_pointer_events,
        force_main_frame_context=_force_main_frame_context,
        restore_iframe_pointer_events=_restore_iframe_pointer_events,
        retry_scroll=_retry_scroll,
        scan_visible_buttons_in_cards=_scan_visible_buttons_in_cards,
        scan_visible_selectors=_scan_visible_selectors,
        safe_page_title=_safe_page_title,
        is_timeout_error=_is_timeout_error,
        to_repo_rel=_to_repo_rel,
    )


def _apply_wait_step(
    page: Any,
    step: WebStep,
    step_num: int,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    *,
    timeout_ms: int,
) -> None:
    _executor_apply_wait_step(
        page=page,
        step=step,
        step_num=step_num,
        actions=actions,
        observations=observations,
        ui_findings=ui_findings,
        timeout_ms=timeout_ms,
        helpers_apply_wait_step=_helpers_apply_wait_step,
        disable_active_youtube_iframe_pointer_events=_disable_active_youtube_iframe_pointer_events,
        force_main_frame_context=_force_main_frame_context,
        restore_iframe_pointer_events=_restore_iframe_pointer_events,
    )


def _launch_browser(
    playwright_obj: Any,
    *,
    visual: bool = False,
    visual_mouse_speed: float = 1.0,
) -> Any:
    kwargs: dict[str, Any] = {"headless": not visual}
    if visual:
        slow_mo = int(max(90, min(500, 260 / max(0.2, visual_mouse_speed))))
        kwargs["slow_mo"] = slow_mo
        kwargs["args"] = [
            "--window-size=1280,860",
            "--window-position=80,60",
        ]
    try:
        return playwright_obj.chromium.launch(channel="chrome", **kwargs)
    except Exception:
        return playwright_obj.chromium.launch(**kwargs)


def _force_visual_overlay_reinstall(page: Any) -> None:
    _visual_force_overlay_reinstall(page)


def _ensure_visual_overlay_ready(page: Any, retries: int = 12, delay_ms: int = 120) -> None:
    _visual_ensure_overlay_ready(page, retries=retries, delay_ms=delay_ms)


def _probe_step_target_state(page: Any, step: WebStep) -> dict[str, Any]:
    return _step_probe_target_state(page, step)


def _interactive_step_not_applicable_reason(page: Any, step: WebStep) -> str:
    return _step_interactive_not_applicable_reason(page, step)


def _is_timeout_error(exc: Exception) -> bool:
    return _step_is_timeout_error(exc)


def _ensure_visual_overlay_ready_best_effort(
    page: Any,
    ui_findings: list[str],
    *,
    cursor_expected: bool,
    retries: int,
    delay_ms: int,
    debug_screenshot_path: Path | None = None,
    force_reinit: bool = False,
) -> bool:
    return _visual_ensure_overlay_ready_best_effort(
        page=page,
        ui_findings=ui_findings,
        cursor_expected=cursor_expected,
        retries=retries,
        delay_ms=delay_ms,
        debug_screenshot_path=debug_screenshot_path,
        force_reinit=force_reinit,
        to_repo_rel=_to_repo_rel,
    )


def _to_repo_rel(path: Path) -> str:
    return _safety_to_repo_rel(path)
