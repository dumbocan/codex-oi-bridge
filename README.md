# codex-oi-bridge

Bridge independiente para usar Open Interpreter (OI) como agente de observación
bajo control de Codex.

## CLI

- `bridge run "<task>"`
- `bridge status`
- `bridge logs --tail 200`

## Contrato JSON

Salida final estricta:
- `task_id`
- `goal`
- `actions[]`
- `observations[]`
- `console_errors[]`
- `network_findings[]`
- `ui_findings[]`
- `result` (`success|partial|failed`)
- `evidence_paths[]`

## Guardrails

- Bloquea intención de edición de código.
- Bloquea comandos destructivos.
- Allowlist de comandos permitidos.
- Confirmación explícita para acciones sensibles.
- Política estricta de acciones: cada item debe ser `cmd: <command>`.
- Validación canónica: `evidence_paths[]` debe quedar dentro de `runs/<run_id>/`.

## Logs y artefactos

Cada ejecución guarda artefactos en `runs/<run_id>/`:
- `bridge.log`
- `oi_stdout.log`
- `oi_stderr.log`
- `prompt.json`
- `report.json`

`bridge logs` incluye tail de `bridge.log`, `oi_stdout.log` y `oi_stderr.log`.

## Security Posture

- No confiar en `report.json` sin validación adicional aguas abajo.
- Consumidores deben ignorar cualquier `evidence_paths` fuera de `run_dir`.

## Notas de versión

- `v0.1.0`: MVP inicial funcional.
- `v0.1.1`: cierre de riesgos de seguridad/operación:
  - hard-fail de `actions[]` no `cmd:`
  - validación canónica de `evidence_paths[]`
  - inclusión de `oi_stdout.log` en `bridge logs`

Ver handoff completo en `docs/CODEX_HANDOFF.md`.
