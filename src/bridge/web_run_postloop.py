"""Post-loop handoff/learning and cleanup helpers for web-run."""

from __future__ import annotations

from typing import Any, Callable


def process_post_loop_handoff_and_learning(
    *,
    page: Any,
    session: Any,
    visual: bool,
    run_state: Any,
    learning_context: dict[str, str],
    learning_window_seconds: int,
    run_dir: Any,
    actions: list[str],
    observations: list[str],
    ui_findings: list[str],
    evidence_paths: list[str],
    teaching_release_control_for_handoff: Callable[..., bool],
    teaching_process_learning_window: Callable[..., None],
    capture_manual_learning: Callable[..., dict[str, Any] | None],
    stable_selectors_for_target: Callable[..., list[str]],
    store_learned_selector: Callable[..., None],
    store_learned_scroll_hints: Callable[..., None],
    write_teaching_artifacts: Callable[..., list[str]],
    show_learning_thanks_notice: Callable[..., None],
    resume_after_learning: Callable[..., bool],
    notify_learning_state: Callable[..., None],
    update_top_bar_state: Callable[..., None],
    session_state_payload: Callable[..., dict[str, Any]],
    disable_active_youtube_iframe_pointer_events: Callable[..., Any],
    restore_iframe_pointer_events: Callable[..., None],
    mark_controlled: Callable[..., None],
    safe_page_title: Callable[[Any], str],
    set_assistant_control_overlay: Callable[[Any, bool], None],
    set_learning_handoff_overlay: Callable[[Any, bool], None],
    set_user_control_overlay: Callable[[Any, bool], None],
) -> None:
    if run_state.release_for_handoff and run_state.handoff_reason != "stuck" and session is not None:
        run_state.control_enabled = teaching_release_control_for_handoff(
            page=page,
            session=session,
            visual=visual,
            control_enabled=run_state.control_enabled,
            wait_for_human_learning=run_state.wait_for_human_learning,
            actions=actions,
            ui_findings=ui_findings,
            mark_controlled=mark_controlled,
            safe_page_title=safe_page_title,
            notify_learning_state=notify_learning_state,
            learning_window_seconds=learning_window_seconds,
            set_assistant_control_overlay=set_assistant_control_overlay,
            set_learning_handoff_overlay=set_learning_handoff_overlay,
            set_user_control_overlay=set_user_control_overlay,
            update_top_bar_state=update_top_bar_state,
            session_state_payload=session_state_payload,
        )

    if run_state.wait_for_human_learning:
        teaching_process_learning_window(
            page=page,
            session=session,
            failed_target_for_teaching=run_state.failed_target_for_teaching,
            learning_context=learning_context,
            learning_window_seconds=learning_window_seconds,
            actions=actions,
            observations=observations,
            ui_findings=ui_findings,
            evidence_paths=evidence_paths,
            capture_manual_learning=capture_manual_learning,
            stable_selectors_for_target=stable_selectors_for_target,
            store_learned_selector=store_learned_selector,
            store_learned_scroll_hints=store_learned_scroll_hints,
            write_teaching_artifacts=lambda payload: write_teaching_artifacts(run_dir, payload),
            show_learning_thanks_notice=show_learning_thanks_notice,
            resume_after_learning=resume_after_learning,
            notify_learning_state=notify_learning_state,
            update_top_bar_state=update_top_bar_state,
            session_state_payload=session_state_payload,
            disable_active_youtube_iframe_pointer_events=disable_active_youtube_iframe_pointer_events,
            restore_iframe_pointer_events=restore_iframe_pointer_events,
        )
        set_learning_handoff_overlay(page, False)


def cleanup_after_run(
    *,
    page: Any,
    browser: Any,
    session: Any,
    attached: bool,
    visual: bool,
    keep_open: bool,
    run_state: Any,
    ui_findings: list[str],
    set_learning_handoff_overlay: Callable[[Any, bool], None],
    set_assistant_control_overlay: Callable[[Any, bool], None],
    update_top_bar_state: Callable[..., None],
    session_state_payload: Callable[..., dict[str, Any]],
    mark_controlled: Callable[..., None],
    safe_page_title: Callable[[Any], str],
) -> None:
    try:
        set_learning_handoff_overlay(page, False)
    except Exception:
        pass
    if visual and run_state.control_enabled:
        set_assistant_control_overlay(page, False)
        if session is not None:
            update_top_bar_state(
                page,
                session_state_payload(session, override_controlled=False, learning_active=False),
            )
        ui_findings.append("control released")
    if attached and session is not None:
        mark_controlled(session, False, url=page.url, title=safe_page_title(page))
    if not attached and not keep_open and not run_state.force_keep_open:
        browser.close()
