# codex-oi-bridge

Bridge independiente para usar Open Interpreter (OI) como agente de observación
bajo control de Codex.

## CLI

- `bridge run "<task>"`
- `bridge run --mode gui "<task>"`
- `bridge run --mode web "<task>"`
- `bridge gui-run "<task>"`
- `bridge web-run "<task>"`
- `bridge web-open [--url ...]`
- `bridge web-release --attach <session_id>`
- `bridge web-close --attach <session_id>`
- `bridge status`
- `bridge logs --tail 200`
- `bridge doctor --mode shell|gui|web`
- `bridge web-run --visual "<task>"` (debug visual con overlay, no headless)
  - Flags: `--visual-cursor on|off`, `--visual-click-pulse on|off`,
    `--visual-scale <float>`, `--visual-color "#3BA7FF"`,
    `--visual-human-mouse on|off`, `--visual-mouse-speed <float>`,
    `--visual-click-hold-ms <int>`

## Runtime recomendado (obligatorio)

Para evitar errores de `OPENAI_API_KEY` ausente por sesión, usar siempre `bridge-safe`:

- Script: `/home/micasa/codex-oi-bridge/bridge-safe`
- Alias shell: `bridge-safe() { /home/micasa/codex-oi-bridge/bridge-safe "$@"; }`

Flujo recomendado:
1. `bridge-safe doctor --mode shell`
2. `bridge-safe run ...` / `bridge-safe web-run ...` / `bridge-safe gui-run ...`

No usar `bridge` directo salvo que la sesión tenga `.venv` y `.env` cargados manualmente.

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

## Web Mode (Playwright) (v1.3)

- Activación: `bridge run --mode web ...` o `bridge web-run ...`.
- Por defecto: ejecución headless (rápida) para runs normales.
- Visual debug opcional: `--visual` para abrir navegador visible con overlay de cursor/click por paso.
- Mouse humano opcional: trayectoria visible + `mousedown` hold + `mouseup`.
- Backend determinista desde bridge (no depende del output narrativo de OI).
- Capacidades:
  - abrir URL explícita del task,
  - click por texto o selector,
  - verificación visible por paso,
  - captura `before/after` por cada click.
- En `--verified` exige:
  - evidencia `before/after` existente y no vacía por step,
  - verify post-step en findings.

Evidencia web por click `N`:
- `runs/<run_id>/evidence/step_<N>_before.png`
- `runs/<run_id>/evidence/step_<N>_after.png`

Hallazgos runtime:
- `console_errors[]` desde consola del navegador.
- `network_findings[]` desde responses >= 400 y requests fallidas.

Pasos web soportados (nativos):
- `click` por texto.
- `click selector:"..."`.
- `select ... from selector "..."` por label/value.
- `wait selector:"..."`.
- `wait text:"..."`.

Nota sobre `wait text`:
- Si hay colisiones con texto oculto (por ejemplo `<option>` en un `<select>`), preferir `wait selector:"..."` con un selector único.

## Persistent Web Session + Control Handoff

- `bridge web-open --url "http://127.0.0.1:5180"`: abre/reusa sesión persistente y devuelve `session_id`.
- `bridge web-run --attach <session_id> ...`: ejecuta en la misma ventana/sesión.
- `bridge web-run --keep-open ...`: crea sesión persistente implícita y no cierra al finalizar.
- `bridge web-release --attach <session_id>`: libera control asistente (quita borde azul).
- `bridge web-close --attach <session_id>`: cierra sesión explícitamente.

Mientras el asistente controla una sesión web visual, aparece borde azul con etiqueta `ASSISTANT CONTROL`.
Al terminar run/release se retira y se registra `control released`.

Session Top Bar (Fase 1.1):
- La barra superior usa un agente local persistente por sesión (`web-open` lo inicia).
- Botones `Refresh`, `Release`, `Close` siguen funcionando aunque `web-run` ya haya terminado.
- La barra se muestra al acercar el cursor al borde superior (hot area) y tiene animación suave de entrada/salida.

Session Observer (Fase 2):
- La barra hace polling de estado (`GET /state`) y publica eventos (`POST /event`) al agente.
- `web-open` inyecta automáticamente la top bar en la página actual (sin necesitar `web-run --visual`).
- Colores:
  - Verde: `READY FOR MANUAL TEST` (`open`, `controlled=false`, `agent_online=true`, `incident_open=false`).
  - Azul: control asistente (`controlled=true`).
  - Rojo: incidente abierto (`incident_open=true`).
  - Gris: sesión abierta en control usuario.
- Acción `Clear incident` (ack):
  - envía `POST /action` con `action=ack`,
  - apaga estado rojo sin cerrar la sesión,
  - mantiene traza (`ack_count`, `last_ack_at`).

Indicador verde (badge):
- Muestra un badge verde sólido con `● READY FOR MANUAL TEST` (alto contraste).

Notas operativas:
- Interacción manual (click/move/resize) está soportada durante sesiones persistentes.
- La sesión solo debe cerrarse con `bridge web-close --attach <session_id>`.
- Si una sesión muere, `bridge status` lo reflejará como `closed`; recuperación: ejecutar `bridge web-open` de nuevo.
- Si la barra muestra `agent offline`, recrear la sesión con `bridge web-open` o cerrar con `bridge web-close --attach <id>`.

Flujo recomendado Fase 2:
1. `bridge web-open --url "http://127.0.0.1:5180"`
2. `bridge web-run --attach <session_id> --keep-open --visual "..."`
3. Forzar un error de UI/red para abrir incidente.
4. Pulsar `Clear incident` en top bar.
5. `bridge status` para confirmar `incident_open=false` con sesión `open`.

Modo push (terminal):
- `bridge watch --attach <session_id>` imprime eventos nuevos e incidentes sin ejecutar `status` manualmente.
- Ejemplo: `bridge watch --attach last --interval-ms 800 --since-last --only warn --notify`.

## Window Management (v1.3)

En `gui` mode el bridge soporta operaciones deterministas de ventanas:
- `window:list`
- `window:active`
- `window:activate <title|id>`
- `window:open <app/url>`

Estas operaciones generan evidencia por paso:
- screenshot before/after
- `step_<N>_window.txt`

## Live Terminal (nuevo)

Para ver en **un solo terminal** lo que decide/hace OI:

```bash
cd /home/micasa/codex-oi-bridge
./bridge-safe live --attach last --interval-ms 600 --tail 40
```

Opcional JSON streaming:

```bash
./bridge-safe live --attach last --json
```

`live` combina:
- estado/progreso del run,
- eventos del observer (click/error/warn),
- nuevas líneas de `bridge.log`, `oi_stdout.log`, `oi_stderr.log`.

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

## Playbook Web ejemplo

```bash
cd /home/micasa/codex-oi-bridge
set -a && source .env && set +a
bridge doctor --mode web
bridge web-run --verified \
  "abre http://localhost:5173, haz click en botón \"Entrar demo\", verifica cambio visible y reporta"
bridge status
bridge logs --tail 200
```

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
- `v1.3.0`: Web + Window control:
  - `--mode web` y `web-run` con Playwright determinista
  - captura console/network real del navegador
  - operaciones de ventana deterministas en `gui` (`window:*`)
- `v1.3.1`: estabilidad de web mode:
  - fix de parseo de URL con puntuación final (`trailing punctuation`) en tasks (`http://... ,`)
- `v1.4.0`: Visual Debug Mode:
  - flag `--visual` en `web-run` / `run --mode web`
  - navegador visible con overlay de click/cursor
  - modo headless actual se mantiene como default para runs rápidos

Ver handoff completo en `docs/CODEX_HANDOFF.md`.

## Histórico de fallos y arreglos (sesiones reales)

- 2026-02-16: `web-run --attach` fallaba con `Attached session is not alive` por sesión cerrada/obsoleta.
  - Arreglo aplicado: flujo operativo con `web-open` nuevo `session_id` + validación de estado antes de attach.
- 2026-02-16: runs visuales quedaban colgados en `web step 1/4` o `web step 2/5` con `controlled=true` sin avanzar.
  - Causa: clicks interactivos sin timeout duro y retries no acotados en targets dinámicos.
  - Arreglo aplicado: `BRIDGE_WEB_INTERACTIVE_TIMEOUT_SECONDS` (default 8s, clamp 1-60s) y manejo de timeout para pasos interactivos en el loop principal.
- 2026-02-16: doble click de login (`Entrar demo`) cuando ya venía en el prompt.
  - Causa: inserción automática del paso demo + paso explícito del task.
  - Arreglo aplicado: deduplicación con `_task_already_requests_demo_click(...)` para no insertar auto-step si el task ya lo incluye.
- 2026-02-16: guardrail bloqueaba runs con `missing required evidence for click step ...` tras timeout de click.
  - Causa: `actions[]` registraba click antes de ejecutarlo; al fallar por timeout no existía `step_after` y la validación contaba el click igual.
  - Arreglo aplicado: acciones interactivas se registran solo después de ejecutar con éxito el click/select.
- 2026-02-16: overlay/cursor visual no siempre visible en attach.
  - Causa: estado del overlay no determinista en navegación/attach.
  - Arreglo aplicado: reinstalación best-effort con reintentos, validación de visibilidad y degradación sin abortar run.
- 2026-02-16: usuario quedaba con borde azul/verde por control retenido tras run atascado.
  - Arreglo aplicado: uso de `web-release --attach <session_id>` y endurecimiento del flujo de liberación de control.

## Estado actual tras estos arreglos

- No hay duplicación de `Entrar demo` si el prompt ya lo pide.
- Los pasos interactivos fallan rápido por timeout, no se quedan colgados indefinidamente.
- La sesión attach se mantiene reutilizable y liberable (`web-release`) sin cerrar ventana.
- La validación de evidencia ya no penaliza clicks no ejecutados por timeout.

## Modelo mental: OI + Bridge + App

### Qué es Open Interpreter (OI)

- OI es un agente local con capacidad de ejecutar acciones en el equipo.
- Puede, según configuración/permisos/herramientas disponibles:
  - ejecutar comandos de terminal,
  - leer/escribir archivos,
  - lanzar procesos,
  - automatizar navegador o GUI.
- OI no es “inteligente por sí mismo”: la planificación la aporta el modelo LLM configurado.

### Rol de la API key de OpenAI

- `OPENAI_API_KEY` conecta OI con el modelo de OpenAI (capa de razonamiento).
- Sin modelo/API válida, OI pierde capacidad de decisión y ejecución guiada.

### Quién manda en esta arquitectura

- `audio3`: producto (backend/frontend), no controla OI.
- `codex-oi-bridge`: orquestador y capa de seguridad/validación.
- OI: ejecutor de acciones locales.
- Modelo OpenAI: razonamiento para decidir pasos.

### Seguridad y control real (por qué no es OI “libre”)

- En este proyecto OI está acotado por guardrails del bridge:
  - allowlist de comandos,
  - bloqueo de comandos destructivos,
  - validación de evidencias,
  - validación de `actions[]` en formato `cmd:`,
  - control/release explícito de sesión web.

### ¿OI “ve” la pantalla y debería navegar solo?

- Sí, puede navegar por la ventana cuando el backend/mode expone señales suficientes (DOM/selectores, eventos, estado de sesión, evidencias).
- No siempre basta “ver la pantalla”: para robustez necesita también anclas deterministas (selectores, texto estable, estado).
- Por eso el diseño actual combina:
  - observación visual,
  - pasos guiados por prompt,
  - validación de resultado,
  - timeouts y fallos rápidos en vez de cuelgues.

### Regla operativa recomendada

- La inteligencia decide, pero el bridge verifica.
- Si una acción no es verificable, se degrada o falla explícitamente.
- Mejor un `failed` rápido y trazable que un run colgado sin diagnóstico.
