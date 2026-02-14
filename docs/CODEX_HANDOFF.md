# CODEX HANDOFF: `codex-oi-bridge`

## 1) Objetivo

`codex-oi-bridge` es una herramienta independiente para usar Open Interpreter (OI)
como **agente operador de observación** bajo control de Codex.

Contrato de roles:
- Codex: decide estrategia, toca código, interpreta resultados.
- OI: ejecuta tareas operativas/observación y devuelve evidencia estructurada.
- OI **no** edita código ni toma decisiones de arquitectura.

## 2) Estado actual

MVP funcional, probado en ejecución real.

Comandos CLI:
- `bridge run "<task>"`
- `bridge status`
- `bridge logs --tail 200`

## 3) Flujo operativo (E2E)

1. `bridge run` crea un `run_id` y carpeta en `runs/<run_id>/`.
2. Genera prompt estricto de observación para OI.
3. Ejecuta OI en modo no interactivo (`--stdin --plain`).
4. Captura `stdout/stderr` de OI.
5. Parsea salida y normaliza a JSON contrato.
6. Ejecuta guardrails sobre acciones reportadas.
7. Guarda artefactos (`report.json`, logs, estado global).

## 4) Contrato JSON (obligatorio)

El `report.json` final siempre queda en este formato:

- `task_id: string`
- `goal: string`
- `actions: string[]`
- `observations: string[]`
- `console_errors: string[]`
- `network_findings: string[]`
- `ui_findings: string[]`
- `result: "success" | "partial" | "failed"`
- `evidence_paths: string[]`

Regla:
- El bridge normaliza salida imperfecta de OI al contrato estable.

## 5) Guardrails activos

- Bloqueo de intención de edición de código.
- Bloqueo de comandos destructivos.
- Allowlist para comandos de observación.
- Confirmación explícita para acciones sensibles.
- Validación post-run de `actions` (formato `cmd: ...`).

## 6) Artefactos de ejecución

Por run:
- `runs/<run_id>/prompt.json`
- `runs/<run_id>/oi_stdout.log`
- `runs/<run_id>/oi_stderr.log`
- `runs/<run_id>/bridge.log`
- `runs/<run_id>/report.json`

Global:
- `runs/status.json`

## 7) Variables de entorno

- `OPENAI_API_KEY` (obligatoria para modo cloud).
- `OI_BRIDGE_COMMAND` (default: `interpreter`).
- `OI_BRIDGE_ARGS` (recomendado: `-y`).
- `OI_BRIDGE_TIMEOUT_SECONDS` (default: `300`).

## 8) Problemas resueltos en el MVP

- OI interactivo bloqueaba el flujo -> modo no interactivo.
- Compatibilidad `--yes` variable -> usar `-y`.
- OI devolvía JSON inconsistente -> parser robusto + normalización.
- Timeouts ruidosos -> manejo controlado.
- Colisión de `run_id` -> carpeta única con sufijo.
- Intentos de API display no disponible -> prompt endurecido para shell.

## 9) Integración desde otro proyecto/Codex

Pasos mínimos:
1. `cd /home/micasa/codex-oi-bridge`
2. `./bridge run "<tarea de observación>"`
3. `./bridge status` para localizar último run.
4. Consumir `runs/<run_id>/report.json` como contrato.

Regla de integración:
- Tratar OI como sensor/operador, no como autor de cambios de código.

## 10) Comandos de smoke test

```bash
export OPENAI_API_KEY="..."
export OI_BRIDGE_ARGS="-y"
export OI_BRIDGE_TIMEOUT_SECONDS=90
./bridge run "inspecciona la UI actual y reporta hallazgos de consola/red/UI"
./bridge status
./bridge logs --tail 200
```

## 11) Checklist para próximo Codex

- Verificar `bridge status` sin errores.
- Ejecutar un `bridge run` corto.
- Confirmar existencia de `report.json` válido.
- Confirmar que guardrails siguen bloqueando acciones prohibidas.
- No ampliar alcance a edición de código sin rediseño explícito.
