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
    `--visual-click-hold-ms <int>`, `--teaching`

## Runtime recomendado (obligatorio)

Para evitar errores de `OPENAI_API_KEY` ausente por sesión, usar siempre `bridge-safe`:

- Script: `/home/micasa/codex-oi-bridge/bridge-safe`
- Alias shell: `bridge-safe() { /home/micasa/codex-oi-bridge/bridge-safe "$@"; }`

Flujo recomendado:
1. `bridge-safe doctor --mode shell`
2. `bridge-safe run ...` / `bridge-safe web-run ...` / `bridge-safe gui-run ...`

No usar `bridge` directo salvo que la sesión tenga `.venv` y `.env` cargados manualmente.

## App Semantics Policy

- `codex-oi-bridge` mantiene motor **genérico** (acciones, prechecks, skip, retries, teaching/handoff, reporting).
- La semántica específica de cada app (flujos, estado login, selectores de negocio) vive en el repositorio de esa app.
- Patrón recomendado de integración (app-aware, fuera del core):
  1. `README` de la app (contrato operativo).
  2. `PLAYBOOK` de la app (mapa de pantallas/estados, reglas de decisión, selectores preferidos/fallbacks, validaciones visibles).
  3. Wrapper semántico de la app (traduce intents cortos como `play song` a un prompt `web-run` robusto).
  4. `state probe` previo (detección de pantalla actual) antes de expandir el prompt semántico.
- Regla de diseño: el core ejecuta pasos y aprende selectores/scrolls; la app decide *qué* pasos aplicar según el estado detectado.
- Para Audio3, el playbook oficial está en:
  - `/home/micasa/audio3/docs/11-OI-PLAYBOOK.md`
- Ejemplo Audio3 (app-side):
  - Wrapper semántico: `/home/micasa/audio3/scripts/oi_semantic_web_run.sh`
  - Traductor de intents: `/home/micasa/audio3/scripts/oi_semantic_prompt.py`
  - El wrapper hace `bridge-safe web-run --verified` como `state probe` para distinguir `landing_demo_or_auth` vs `catalog_ready` antes de generar el plan.
- Contrato de lectura para ejecutar Audio3 con OI:
  1. `/home/micasa/audio3/README.md`
  2. `/home/micasa/audio3/docs/11-OI-PLAYBOOK.md`
  3. `/home/micasa/audio3/AGENTS.md`
  - Precedencia: `AGENTS.md > README.md > PLAYBOOK`.

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

Modo enseñanza (`--teaching`):
- Si falla un target interactivo, intenta selector estable + scroll (contenedor principal y página) con hasta 2 reintentos.
- Si sigue fallando, muestra aviso en la UI y cede control automático (`release`) manteniendo la ventana abierta.
- Si detecta atasco (paso interactivo sin avance / sin eventos útiles), hace handoff proactivo con aviso:
  `Me he atascado en: <paso>. Te cedo el control para que me ayudes.`
- Política `main-frame-first`: antes de cada paso interactivo/wait, el executor fuerza contexto al frame principal (salida de iframe, `Escape`, foco/click noop en `document.body`).
- Nuevo motivo de atasco `stuck_iframe_focus`: si queda foco/cursor en iframe (p.ej. YouTube) > 8s sin progreso útil, desactiva temporalmente `pointer-events` del iframe activo, reintenta en main frame y, si no recupera, lanza handoff normal a `USER CONTROL` (verde) con `release` y `keep-open`.
- Observa clicks manuales del usuario, guarda artefacto local en `runs/<run_id>/learning/` y prioriza selectores aprendidos en ejecuciones futuras para el mismo estado.
- Ventana de aprendizaje configurable con `BRIDGE_LEARNING_WINDOW_SECONDS` (default `25`).
- Verbosidad del observer configurable con `BRIDGE_OBSERVER_NOISE_MODE=minimal|debug` (default `minimal`).
- Timeout duro por paso `BRIDGE_WEB_STEP_HARD_TIMEOUT_SECONDS` (default `20`) para evitar runs colgados en interacción.
- Timeout duro global `BRIDGE_WEB_RUN_HARD_TIMEOUT_SECONDS` (default `120`) para forzar cierre del run con reporte consistente.

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
  - Azul: control asistente (`controlled=true`).
  - Naranja: aprendizaje/handoff (`learning_active=true`).
  - Verde: control usuario (`controlled=false`, `learning_active=false`).
  - Rojo: incidente abierto (`incident_open=true`).
  - Gris: sesión abierta pero sin canal de control/manual listo.
- Acción `Clear incident` (ack):
  - envía `POST /action` con `action=ack`,
  - apaga estado rojo sin cerrar la sesión,
  - mantiene traza (`ack_count`, `last_ack_at`).

Indicador verde (badge):
- Muestra un badge verde sólido con `● READY FOR MANUAL TEST` (alto contraste).

Notas operativas:
- En `BRIDGE_OBSERVER_NOISE_MODE=minimal`, en `USER CONTROL` normal no se registran `mousemove/scroll/click` triviales; sí se registran errores (`console_error`, `page_error`, `network_error` relevante).
- En `BRIDGE_OBSERVER_NOISE_MODE=debug`, se habilita traza extensa para diagnóstico puntual.
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

## Progreso reciente (teaching mode)

- `--teaching` en `web-run`/`run --mode web` con reintentos guiados (selector estable + scroll + evidencia retry).
- Handoff automático por atasco real (watchdog global de step/progreso), incluso sin excepción en el paso.
- Handoff unificado: aviso visible, estado `LEARNING/HANDOFF` naranja, `release` automático, `keep-open` efectivo.
- Registro explícito en `ui_findings` cuando hay atasco:
  - `what_failed=stuck`
  - `where=<step>`
  - `attempted=<...>`
  - `next_best_action=human_assist`
- Aprendizaje post-handoff:
  - captura de acción manual y artefactos en `runs/<run_id>/learning/teaching_*.json|md`
  - persistencia global en `runs/learning/web_teaching_selectors.json`
  - reutilización del selector aprendido en runs siguientes del mismo contexto.
- UX de ayuda humana:
  - pulso/cursor visible al click manual (`manual click captured`),
  - filtro de clicks irrelevantes (evita aprender botones del topbar),
  - aviso de click incorrecto con sugerencia de selector objetivo,
  - mensaje de confirmación: `Gracias, ya he aprendido... Ya continúo yo.`,
  - intento de auto-resume tras aprender (`learning-resume`).
- Antiruido de observabilidad:
  - `minimal` evita ruido en `USER CONTROL` libre y no cuenta `mousemove/scroll` triviales como progreso útil,
  - `debug` habilita telemetría amplia para debugging.

## Playbook reproducible: movimiento visual + Stop (Audio3)

Este flujo es el que se validó en sesión real para ver cursor/colores y ejecutar `Stop`.

Precondiciones:
- App Audio3 levantada en `http://127.0.0.1:5181`.
- Usar siempre `bridge-safe` (carga runtime/entorno correcto).

Comando exacto (visual + teaching + keep-open):

```bash
./bridge-safe web-run --visual --visual-cursor on --visual-click-pulse on --visual-human-mouse on --visual-mouse-speed 0.75 --visual-click-hold-ms 180 --teaching --keep-open "open http://127.0.0.1:5181, click selector '#track-play-track-stan', wait text:'Now playing:', click selector '#player-stop-btn', verify visible result"
```

Verificación esperada:
- Se ve borde/estado visual en top bar (`ASSISTANT CONTROL` azul cuando controla el asistente).
- Se ve cursor/pulso de click.
- En `report.json` aparecen acciones:
  - `cmd: playwright visual on`
  - `cmd: playwright click selector:#track-play-track-stan`
  - `cmd: playwright wait text:Now playing:`
  - `cmd: playwright click selector:#player-stop-btn`

Evidence:
- `runs/<run_id>/report.json`
- `runs/<run_id>/evidence/step_*_before.png`
- `runs/<run_id>/evidence/step_*_after.png`

Comprobación rápida de resultado:

```bash
cat runs/status.json
```

Debe terminar con `state: "completed"` (nunca quedarse en `running`).

### Playbook específico: mouse humano (el que nos costó estabilizar)

Objetivo:
- Ver trayectoria de puntero + click humano (hold/release) de forma consistente.

Comando recomendado:

```bash
./bridge-safe web-run --visual --visual-cursor on --visual-click-pulse on --visual-human-mouse on --visual-mouse-speed 0.75 --visual-click-hold-ms 180 --teaching --keep-open "open http://127.0.0.1:5181, click selector '#player-stop-btn'"
```

Ajustes que sí funcionaron:
- `--visual-human-mouse on`: activa movimiento humano, no click instantáneo.
- `--visual-mouse-speed 0.75`: evita movimiento excesivamente lento (atasca) o demasiado rápido (sin feedback claro).
- `--visual-click-hold-ms 180`: `mousedown`/`mouseup` visible y estable.
- `--visual-cursor on` + `--visual-click-pulse on`: feedback visual claro.

Señales de que está bien:
- El cursor se desplaza hasta el target (no “teletransporte”).
- Se ve pulso al click.
- En `report.json` hay acción de click del target esperado.

Si falla:
- confirmar `cmd: playwright visual on` en `report.json`,
- reducir complejidad del prompt a un solo click,
- repetir sin `--keep-open` para aislar estado viejo de sesión,
- cerrar sesión previa con `./bridge-safe web-close --attach <session_id>`.

### Fallos reales que tuvimos y cómo evitarlos

1. Overlay de color no visible aunque el click sí funcionaba.
- Causa: capa visual/overlay no siempre se inyectaba en ciertos runs.
- Prevención: ejecutar con `--visual --visual-cursor on --visual-click-pulse on` y verificar en `report.json` `cmd: playwright visual on`.

2. Run atascado en `wait text:'Now playing:'` y no llegaba a `Stop`.
- Causa: espera estricta en un estado frágil de reproducción.
- Prevención: teaching con soft-skip de ese wait cuando el siguiente objetivo es `Stop`.

3. Handoff aprendía una clave basura tipo `step 3/4 wait_text:...`.
- Causa: target de learning tomado desde firma de paso, no desde objetivo interactivo.
- Prevención: learning solo con target interactivo real (`#player-stop-btn`, `Stop`).

4. Click manual tras handoff no se capturaba (`learning_capture=none`).
- Causa: ventana/estado de learning no mantenido correctamente o click filtrado.
- Prevención: `learning_active=true` durante ventana de aprendizaje y captura explícita de click manual útil.

5. Foco atrapado en iframe YouTube.
- Causa: contexto en iframe durante acción de app.
- Prevención: política `main-frame-first`, desactivar `pointer-events` en iframe activo y reintentar en DOM principal; si no recupera, handoff automático.

6. Runs quedaban en `running` sin `report.json`.
- Causa: falta de cierre duro en ciertos atascos/timeouts.
- Prevención: timeout duro por paso/run + finalización garantizada con `report.json` consistente.

7. Sesiones abiertas acumuladas tras pruebas visuales.
- Causa: uso de `--keep-open` sin cierre manual.
- Prevención: cerrar siempre al terminar pruebas:

```bash
./bridge-safe web-close --attach <session_id>
```

## Resumen consolidado de lo implementado

- Web teaching robusto:
  - retries con selector estable + scroll y evidencia before/after,
  - handoff automático por `target_not_found`, `stuck`, `stuck_iframe_focus`, `interactive_timeout`, `run_timeout`,
  - `keep-open` efectivo en handoff.
- Watchdog global:
  - detección por step sin cambio y por falta de progreso útil,
  - no depende de excepción para ceder control.
- Aprendizaje real:
  - captura click manual útil (selector/text/url/timestamp),
  - captura scroll manual relevante durante handoff (para contexto de búsqueda),
  - persistencia en `runs/<run_id>/learning/` y `runs/learning/web_teaching_selectors.json`,
  - reutilización en runs siguientes por contexto.
  - pre-scroll aprendido antes de reintentos del mismo objetivo (cuando hay hints guardados).
- Main-frame-first/iframe:
  - salida activa de foco iframe,
  - guard temporal `pointer-events: none` en iframe YouTube durante handoff/learning.
- Antiruido observabilidad:
  - `BRIDGE_OBSERVER_NOISE_MODE=minimal|debug`,
  - en `minimal` no se cuentan eventos triviales como progreso útil.
- Cierre garantizado:
  - `report.json` y `status.json` consistentes al finalizar,
  - run no queda en `running` indefinido.
- Refactor incremental en progreso (sin romper tests):
  - extracción a módulos: parser, bulk scan, teaching, watchdog, handoff, frame-guard, preflight, step-runner, retries, learning-store, handoff-actions, interaction-executor, step-applicability, visual-runtime, runtime-safety, run-bootstrap, run-state, session-overlay-ops, run-postloop, target-preflight, run-reporting, run-loop, finalización.
  - `web_backend.py` reducido progresivamente con wrappers de compatibilidad para no romper tests existentes (`1172` líneas en esta ronda).
  - validación repetida durante el refactor:
    - `flake8 -j1 src/bridge/web_backend.py src/bridge/web_run_loop.py src/bridge/web_run_reporting.py src/bridge/web_target_preflight.py src/bridge/web_run_postloop.py src/bridge/web_session_overlay_ops.py src/bridge/web_run_state.py src/bridge/web_run_bootstrap.py src/bridge/web_runtime_safety.py src/bridge/web_visual_runtime.py src/bridge/web_step_applicability.py src/bridge/web_interaction_executor.py src/bridge/web_learning_store.py src/bridge/web_preflight.py src/bridge/web_step_runner.py src/bridge/web_teaching.py src/bridge/web_run_handoff.py src/bridge/web_interactive_retries.py src/bridge/web_handoff_actions.py`
    - `PYTHONPATH=src python3 -m unittest -q tests.test_web_mode tests.test_web_session tests.test_live tests.test_cli tests.test_web_backend` (76 tests)

## Estructura de carpetas (refactor en progreso)

Nota:
- Esta sección refleja el estado actual. Cuando cerremos la refactor completa, se actualizará con el árbol final estable.

Módulos principales en `src/bridge/`:
- `web_backend.py`: orquestación principal de ejecución web (todavía en reducción progresiva).
- `web_steps.py`: parseo de comandos web y rewrite de pasos genéricos (ej. play ambiguo).
- `web_bulk_scan.py`: escaneo DOM para operaciones bulk (cards, selectors visibles, playlist seleccionada).
- `web_preflight.py`: navegación/contexto inicial, overlay inicial y evidencia `step_0_context`.
- `web_step_runner.py`: prechecks comunes por step y ejecución de ramas `interactive`/`wait` con resultados estructurados.
- `web_interactive_retries.py`: reintentos interactivos con scroll, selector fallback y detección de stuck local.
- `web_interaction_executor.py`: primitivas Playwright de interacción (`click/fill/select/bulk`) y wrapper de waits.
- `web_step_applicability.py`: precheck genérico de aplicabilidad (`present/visible/enabled`) y helper de timeout errors.
- `web_learning_store.py`: persistencia/carga de selectores y scroll-hints aprendidos, y priorización por contexto.
- `web_teaching.py`: captura de aprendizaje manual, validación de click útil, artefactos y resume.
- `web_watchdog.py`: estado/config de watchdog y evaluación de atascos.
- `web_executor_steps.py`: clasificación de tipos de step y findings repetidos de error/timeout.
- `web_run_finalize.py`: normalización de resultado final y estructura de `ui_findings`.
- `web_handoff.py`: avisos y transición de control en handoff teaching.
- `web_handoff_actions.py`: helpers para producir actualizaciones de estado al ceder control (stuck/target_not_found).
- `web_run_handoff.py`: decisiones de handoff por watchdog/timeout/iframe con payload estructurado.
- `web_frame_guard.py`: política main-frame-first y guardias de foco/iframe.
- `web_visual_runtime.py`: robustez de overlay visual (reinstalación, readiness y fallback best-effort).
- `web_runtime_safety.py`: guardias de página/sesión cerrada, evidencia timeout y helpers de path relativos.
- `web_run_bootstrap.py`: setup inicial de browser/page, overlay inicial, observers y timeouts/config de run.
- `web_run_state.py`: estado mutable del run (handoff/control/result) y helpers para aplicar decisiones/updates.
- `web_session_overlay_ops.py`: operaciones sobre top bar/overlay en sesiones attach (release/ensure/destroy) desacopladas del backend.
- `web_run_postloop.py`: procesamiento post-loop (handoff/learning) y cleanup `finally` de sesión/browser.
- `web_target_preflight.py`: comprobaciones de reachability/stack previas a `web-run` con wrappers compatibles en backend.
- `web_run_reporting.py`: persistencia final de `report.json`/`status.json` y ensamblado del `OIReport` final.
- `web_run_loop.py`: orquestación del loop de steps (`interactive`/`wait`) extraída del backend con callbacks para mantener semántica.

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
