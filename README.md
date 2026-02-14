# codex-oi-bridge

Bridge independiente para usar Open Interpreter (OI) como agente de observación
bajo control de Codex.

## CLI

- `bridge run "<task>"`
- `bridge run --mode gui "<task>"`
- `bridge gui-run "<task>"`
- `bridge status`
- `bridge logs --tail 200`
- `bridge doctor --mode shell|gui`

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

## GUI Operator Mode (v1.2)

- Activación: `bridge run --mode gui ...` o `bridge gui-run ...`.
- En GUI mode, `--confirm-sensitive` es obligatorio.
- Allowlist GUI explícita: `xdotool`, `wmctrl`, `xwininfo`, `import`, `scrot` (más comandos shell permitidos).
- Clicks sin ventana objetivo explícita son bloqueados.
- Clicks por coordenadas (`mousemove ... click`) son bloqueados.
- Tras cada click se exige verificación y evidencia before/after.
- Paso previo recomendado/obligatorio en operación diaria: `bridge doctor --mode gui`.

Evidencia obligatoria por click `N`:
- `runs/<run_id>/evidence/step_<N>_before.png`
- `runs/<run_id>/evidence/step_<N>_after.png`
- `runs/<run_id>/evidence/step_<N>_window.txt`

## Logs y artefactos

Cada ejecución guarda artefactos en `runs/<run_id>/`:
- `bridge.log`
- `oi_stdout.log`
- `oi_stderr.log`
- `prompt.json`
- `report.json`

`bridge logs` incluye tail de `bridge.log`, `oi_stdout.log` y `oi_stderr.log`.

## v1.2.3 Runtime Hardening

Open Interpreter now runs with per-run writable directories:
- `HOME=runs/<run_id>/.oi_home`
- `XDG_CACHE_HOME=runs/<run_id>/.oi_home/.cache`
- `XDG_CONFIG_HOME=runs/<run_id>/.oi_home/.config`
- `MPLCONFIGDIR=runs/<run_id>/.oi_home/.config/matplotlib`

This avoids read-only failures (for example `~/.cache/open-interpreter/contribute.json`) and keeps runtime artifacts isolated per run.

## Requisitos de entorno GUI (X11)

- `DISPLAY` válido (por ejemplo `:0`).
- Sesión X11 activa con foco en la ventana esperada.
- Herramientas presentes: `xdotool`, `wmctrl`, `xwininfo`, y para screenshots `import` o `scrot`.

Troubleshooting típico:
- `DISPLAY` no configurado: exportar `DISPLAY=:0` en la sesión correcta.
- Ventana incorrecta en foco: usar pasos explícitos de búsqueda/activación de ventana.
- Sin screenshots: instalar o habilitar `import`/`scrot`.
- Preflight rápido: `bridge doctor --mode gui`.

## Playbook GUI ejemplo

```bash
cd /home/micasa/codex-oi-bridge
set -a && source .env && set +a
bridge doctor --mode gui
bridge gui-run --confirm-sensitive \
  "abre navegador, navega a https://example.com y haz click en botón \"Descargar archivo\". \
verifica resultado visible tras click y guarda evidencia por paso."
bridge status
bridge logs --tail 200
```

Then inspect:
- `runs/<run_id>/report.json`
- `runs/<run_id>/evidence/`

Resultado esperado:
- `actions[]` con comandos `cmd: ...`
- `observations/ui_findings` con ubicación del botón, acción aplicada y cambio visible
- `evidence_paths[]` con before/after/window por cada click

## Security Posture

- No confiar en `report.json` sin validación adicional aguas abajo.
- Consumidores deben ignorar cualquier `evidence_paths` fuera de `run_dir`.

## Notas de versión

- `v0.1.0`: MVP inicial funcional.
- `v0.1.1`: cierre de riesgos de seguridad/operación:
  - hard-fail de `actions[]` no `cmd:`
  - validación canónica de `evidence_paths[]`
  - inclusión de `oi_stdout.log` en `bridge logs`
- `v1.2.0`: GUI Operator Mode:
  - `--mode gui` y `gui-run`
  - guardrails GUI (target window + verify + no coordinate clicks)
  - evidencia obligatoria before/after por click

Ver handoff completo en `docs/CODEX_HANDOFF.md`.
