# Audio3 Project Profile (Lightweight)

## Scope
Perfil operativo mínimo para runs del asistente sobre Audio3.

## Source of truth
Este perfil resume. La documentación completa vive en:
- `/home/micasa/audio3/docs/AUDIO3_UI_MAP.md`
- `/home/micasa/audio3/docs/AUDIO3_STATE_MACHINE.md`
- `/home/micasa/audio3/docs/AUDIO3_TEST_PLAYBOOK.md`

## Runtime policy
1. Detect UI state first (`login` / `catalog` / `player_open` / `incident`).
2. Execute only state-valid actions.
3. Max 2 fallbacks per objective.
4. Interactive steps must fail fast by timeout.
5. Escalate with structured report when blocked.

## Critical anti-patterns to avoid
- Do not click `Entrar demo` when already in `catalog`.
- Do not continue silently after timeout.
- Do not keep control stuck; always release on failure.

## Core checks
- Capture `step_0_context` before first action.
- Require evidence `before/after` or `timeout` per interactive step.
- Validate postcondition on every step.
