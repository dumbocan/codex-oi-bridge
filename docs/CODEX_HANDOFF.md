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

## 12) Known Risks (v0.1.0)

Riesgos detectados en v0.1.0:
1. `actions[]` solo se validaba si empezaba por `cmd:` (posible bypass).
2. `evidence_paths[]` no estaba restringido canónicamente a `runs/<run_id>/`.
3. `bridge logs` no incluía `oi_stdout.log`.

## 13) Security Posture

- No confiar en `report.json` sin validación adicional.
- Consumidores deben ignorar `evidence_paths` fuera de `run_dir`.

## 14) Next Patch Plan (v0.1.1) [COMPLETADO]

Implementado en v0.1.1:
1. Hard-fail si una acción no sigue formato `cmd: ...`.
2. Validación canónica de `evidence_paths[]` dentro de `ctx.run_dir`.
3. `bridge logs` incluye `oi_stdout.log` además de `bridge.log` y `oi_stderr.log`.

## 15) Test Gaps (v0.1.0) [CERRADOS en v0.1.1]

Se añadieron tests para:
- bypass de `actions[]` sin `cmd:`.
- path traversal/rutas externas en `evidence_paths[]`.
- salida de `bridge logs` incluyendo stdout+stderr.

## 16) GUI Operator Mode (v1.2.0)

Nuevos entrypoints:
- `bridge run --mode gui "<task>"`
- `bridge gui-run "<task>"`

Límites del modo GUI:
- Sigue prohibido editar código.
- Sigue bloqueando comandos destructivos.
- `--confirm-sensitive` es obligatorio.
- Click sin ventana objetivo explícita => bloqueado.
- Click por coordenadas => bloqueado.
- Cada click exige verificación post-click en findings.

## 17) Evidencia obligatoria GUI

Por cada click de paso `N`:
- `runs/<run_id>/evidence/step_<N>_before.png`
- `runs/<run_id>/evidence/step_<N>_after.png`
- `runs/<run_id>/evidence/step_<N>_window.txt`

Si falta cualquiera => hard-fail del run.

## 18) Requisitos entorno X11

- `DISPLAY` apuntando a sesión válida.
- Herramientas: `xdotool`, `wmctrl`, `xwininfo`.
- Captura: `import` (ImageMagick) o `scrot`.

## 19) Troubleshooting GUI

- Error de display: revisar `echo $DISPLAY`.
- Click no efectivo: revisar foco y target window explícito.
- Sin capturas: instalar `scrot` o ImageMagick (`import`).
- Botón no encontrado: reportar bloqueo + alternativa segura en `observations`.

## 20) Playbook GUI (ejemplo real)

Objetivo:
- Abrir navegador
- Ir a URL dada
- Click en botón `"Descargar archivo"`
- Verificar resultado visible

Comando:
```bash
bridge gui-run --confirm-sensitive \
  "abre navegador, navega a https://example.com y haz click en botón \"Descargar archivo\". \
verifica resultado visible tras click y guarda evidencia por paso."
```

Validaciones esperadas:
- `actions[]` solo `cmd: ...`
- evidencia before/after/window por cada click
- findings con ubicación del botón + acción + resultado visible

## 21) v1.2.3 Runtime Hardening

Open Interpreter now runs with per-run writable directories:
- `HOME=runs/<run_id>/.oi_home`
- `XDG_CACHE_HOME=runs/<run_id>/.oi_home/.cache`
- `XDG_CONFIG_HOME=runs/<run_id>/.oi_home/.config`
- `MPLCONFIGDIR=runs/<run_id>/.oi_home/.config/matplotlib`

This avoids read-only failures (for example `~/.cache/open-interpreter/contribute.json`) and keeps runtime artifacts isolated per run.

## 22) Recommended GUI Flow

```bash
cd /home/micasa/codex-oi-bridge
set -a && source .env && set +a
bridge doctor --mode gui
bridge gui-run --confirm-sensitive "<gui task>"
bridge status
bridge logs --tail 200
```

Then inspect:
- `runs/<run_id>/report.json`
- `runs/<run_id>/evidence/`

## 23) v1.3.0 Web Mode (Playwright)

Nuevos entrypoints:
- `bridge run --mode web "<task>"`
- `bridge web-run "<task>"`
- `bridge doctor --mode web`

Características:
- ejecución determinista desde bridge (sin depender de texto libre de OI),
- abre URL explícita del task,
- click por texto/selector,
- verificación visible post-step,
- screenshots `step_N_before.png` y `step_N_after.png`,
- captura real de `console_errors[]` y `network_findings[]`.

En `--verified`:
- hard-fail si falta evidencia before/after por step,
- hard-fail si no hay verify post-step.

## 24) v1.3.0 Window Management (GUI)

Soporte explícito en modo `gui`:
- `window:list`
- `window:active`
- `window:activate <title|id>`
- `window:open <app/url>`

Comportamiento:
- si el task trae operaciones `window:*`, las ejecuta el backend determinista del bridge.
- cada paso genera evidencia:
  - `step_N_before.png`
  - `step_N_after.png`
  - `step_N_window.txt`

Guardrails:
- se mantiene bloqueo destructivo,
- se mantiene bloqueo de coordinate-click unsafe,
- se mantiene política de evidencia dentro de `runs/<run_id>/`.

## 25) Flujo recomendado (v1.3.0)

```bash
cd /home/micasa/codex-oi-bridge
set -a && source .env && set +a
bridge doctor --mode web
bridge web-run --verified "<web task>"
bridge doctor --mode gui
bridge gui-run --confirm-sensitive --verified "<gui/window task>"
bridge status
bridge logs --tail 200
```

## 26) v1.3.1 Hotfix

- Fix: URL parsing en `web` mode ahora normaliza puntuación final en lenguaje natural.
- Caso cubierto: tasks como `abre http://localhost:5173, click ...` ya no fallan por URL inválida.

## 27) Runtime recomendado (obligatorio)

Para evitar errores de `OPENAI_API_KEY` ausente por sesión, usar siempre `bridge-safe`:

- Script: `/home/micasa/codex-oi-bridge/bridge-safe`
- Alias shell: `bridge-safe() { /home/micasa/codex-oi-bridge/bridge-safe "$@"; }`

Flujo:
1. `bridge-safe doctor --mode shell`
2. `bridge-safe run ...` / `bridge-safe web-run ...` / `bridge-safe gui-run ...`

No usar `bridge` directo salvo que la sesión tenga `.venv` y `.env` cargados manualmente.

## 28) v1.4.0 Visual Debug Mode

Objetivo:
- depurar interacciones web en vivo (ventana visible + overlay de click/cursor),
- mantener headless actual como default para runs rápidos.

Uso:
- `bridge web-run --visual "<task web>"`
- `bridge run --mode web --visual "<task web>"`
- Flags:
  - `--visual-cursor on|off`
  - `--visual-click-pulse on|off`
  - `--visual-scale <float>`
  - `--visual-color "#3BA7FF"`
  - `--visual-human-mouse on|off`
  - `--visual-mouse-speed <float>`
  - `--visual-click-hold-ms <int>`

Comportamiento:
- en modo visual, Playwright corre headed (no headless),
- instala overlay en la página y marca cada interacción (click/select) por paso,
- cursor azul persistente + pulse por click + estela corta de movimiento,
- click humano opcional (`mousemove` por pasos + `mousedown` hold + `mouseup`),
- `actions[]` incluye `cmd: playwright visual on`,
- se mantiene evidencia before/after y validaciones de `--verified`.

## 29) Persistent Web Session + Control Handoff

Nuevos comandos:
- `bridge web-open [--url ...]`
- `bridge web-run --attach <session_id> ...`
- `bridge web-run --keep-open ...`
- `bridge web-release --attach <session_id>`
- `bridge web-close --attach <session_id>`

Comportamiento:
- Sesiones persistentes guardan estado (`session_id`, `url`, `title`, `controlled`, `last_seen_at`).
- `bridge status` muestra `web_session` cuando existe.
- En control asistente, se muestra overlay global con borde azul + `ASSISTANT CONTROL`.
- Al finalizar o hacer `web-release`, se retira overlay y se registra `control released`.
- Si hay excepción durante run attach/keep-open, el bridge intenta liberar control automáticamente.
- La top bar usa canal persistente (agente local HTTP por sesión), no binding efímero de Playwright.
- La top bar aplica animación `translateY(-110%) -> translateY(0)` y muestra feedback de acción.
- La top bar implementa observabilidad en vivo:
  - `GET /state` (polling),
  - `POST /event` (click/error/network),
  - `POST /action` (`refresh`, `release`, `close`, `ack`).
- `Clear incident` (`action=ack`) limpia estado rojo sin cerrar sesión.

Troubleshooting:
- Move/resize/manual interaction en la ventana persistente está soportada.
- La sesión no debería cerrarse salvo `web-close`; si aparece `closed`, recrear con `web-open`.
- `bridge status` y `runs/web_sessions/<id>.json` se sincronizan por liveness real (PID+CDP) antes de reportar estado.
- Si la barra indica `agent offline`, las acciones se deshabilitan; recuperar con `web-open` (nueva sesión) o `web-close` de la sesión muerta.

Colores de estado en barra:
- Azul: `controlled=true` (assistant control).
- Rojo: `incident_open=true`.
- Gris: sesión `open` bajo control usuario.
