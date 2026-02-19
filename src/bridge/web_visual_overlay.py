"""Visual overlay installer for web-run sessions."""

from __future__ import annotations

import json
from typing import Any


def _install_visual_overlay(
    page: Any,
    *,
    cursor_enabled: bool,
    click_pulse_enabled: bool,
    scale: float,
    color: str,
    trace_enabled: bool,
    session_state: dict[str, Any] | None = None,
) -> None:
    config = {
        "cursorEnabled": bool(cursor_enabled),
        "clickPulseEnabled": bool(click_pulse_enabled),
        "scale": float(scale),
        "color": str(color),
        "traceEnabled": bool(trace_enabled),
    }
    session_json = json.dumps(session_state or {}, ensure_ascii=False)
    script_template = """
    (() => {
      const cfg = __CFG_JSON__;
      const sessionState = __SESSION_JSON__;
      const installOverlay = () => {
        const prevCfgRaw = window.__bridgeOverlayConfig || null;
        const cfgRaw = JSON.stringify(cfg || {});
        const prevRaw = JSON.stringify(prevCfgRaw || {});
        if (window.__bridgeOverlayInstalled && prevRaw !== cfgRaw) {
          const ids = [
            '__bridge_cursor_overlay',
            '__bridge_trail_layer',
            '__bridge_state_border',
            '__bridge_step_badge',
          ];
          ids.forEach((id) => document.getElementById(id)?.remove());
          window.__bridgeOverlayInstalled = false;
        }
        if (window.__bridgeOverlayInstalled) return true;
        window.__bridgeOverlayConfig = cfg;
        const root = document.documentElement;
        const body = document.body;
        if (!root || !body) {
          if (!window.__bridgeOverlayRetryAttached) {
            window.__bridgeOverlayRetryAttached = true;
            document.addEventListener('DOMContentLoaded', () => {
              installOverlay();
            }, { once: true });
          }
          return false;
        }
        const overlayHost = body;
        const cursor = document.createElement('div');
        cursor.id = '__bridge_cursor_overlay';
        cursor.style.position = 'fixed';
        cursor.style.width = `${14 * cfg.scale}px`;
        cursor.style.height = `${14 * cfg.scale}px`;
        cursor.style.border = `${2 * cfg.scale}px solid ${cfg.color}`;
        cursor.style.borderRadius = '50%';
        cursor.style.boxShadow = `0 0 0 ${3 * cfg.scale}px rgba(59,167,255,0.25)`;
        cursor.style.pointerEvents = 'none';
        cursor.style.zIndex = '2147483647';
        cursor.style.background = 'rgba(59,167,255,0.15)';
        cursor.style.display = cfg.cursorEnabled ? 'block' : 'none';
        cursor.style.transition = 'width 120ms ease, height 120ms ease, left 80ms linear, top 80ms linear';
        overlayHost.appendChild(cursor);
        const trailLayer = document.createElement('div');
        trailLayer.id = '__bridge_trail_layer';
        trailLayer.style.position = 'fixed';
        trailLayer.style.inset = '0';
        trailLayer.style.pointerEvents = 'none';
        trailLayer.style.zIndex = '2147483646';
        overlayHost.appendChild(trailLayer);

        const stateBorder = document.createElement('div');
        stateBorder.id = '__bridge_state_border';
        stateBorder.style.position = 'fixed';
        stateBorder.style.inset = '0';
        stateBorder.style.pointerEvents = 'none';
        stateBorder.style.zIndex = '2147483642';
        stateBorder.style.boxSizing = 'border-box';
        stateBorder.style.borderRadius = String(14 * cfg.scale) + 'px';
        stateBorder.style.border = String(6 * cfg.scale) + 'px solid rgba(210,210,210,0.22)';
        stateBorder.style.boxShadow = '0 0 0 1px rgba(0,0,0,0.28) inset';
        stateBorder.style.transition =
          'border-color 180ms ease-out, box-shadow 180ms ease-out, ' +
          'border-width 180ms ease-out';
        overlayHost.appendChild(stateBorder);

        window.__bridgeSetStateBorder = (state) => {
          const s = state || {};
          const controlled = !!s.controlled;
          const open = String(s.state || 'open') === 'open';
          const incidentOpen = !!s.incident_open;
          const learningActive = !!s.learning_active;
          const controlUrl = window.__bridgeResolveControlUrl ? window.__bridgeResolveControlUrl(s) : null;
          const agentOnline = !!controlUrl && s.agent_online !== false;
          const readyManual = open && !controlled && agentOnline && !incidentOpen && !learningActive;

          let color = 'rgba(210,210,210,0.22)';
          let glow = '0 0 0 1px rgba(0,0,0,0.28) inset';
          if (!open) {
            color = 'rgba(40,40,40,0.55)';
            glow = '0 0 0 1px rgba(0,0,0,0.35) inset';
          } else if (controlled) {
            color = 'rgba(59,167,255,0.95)';
            glow = '0 0 0 2px rgba(59,167,255,0.35) inset, 0 0 26px rgba(59,167,255,0.22)';
          } else if (incidentOpen) {
            color = 'rgba(255,82,82,0.95)';
            glow = '0 0 0 2px rgba(255,82,82,0.32) inset, 0 0 26px rgba(255,82,82,0.18)';
          } else if (learningActive) {
            color = 'rgba(245,158,11,0.95)';
            glow = '0 0 0 2px rgba(245,158,11,0.30) inset, 0 0 26px rgba(245,158,11,0.18)';
          } else if (readyManual) {
            color = 'rgba(34,197,94,0.95)';
            glow = '0 0 0 2px rgba(34,197,94,0.32) inset, 0 0 26px rgba(34,197,94,0.18)';
          } else {
            color = 'rgba(210,210,210,0.22)';
            glow = '0 0 0 1px rgba(0,0,0,0.28) inset';
          }

          const emphasized = (controlled || incidentOpen || readyManual);
          stateBorder.style.borderWidth = String((emphasized ? 10 : 6) * cfg.scale) + 'px';
          stateBorder.style.borderColor = color;
          stateBorder.style.boxShadow = glow;
          window.__bridgeOverlayState = {
            controlled,
            incidentOpen,
            learningActive,
            readyManual,
          };
        };

        let lastTrailPoint = null;
        const emitTrail = (x, y) => {
        if (!cfg.cursorEnabled) return;
        const px = Number(x);
        const py = Number(y);
        if (!Number.isFinite(px) || !Number.isFinite(py)) return;
        if (lastTrailPoint && Number.isFinite(lastTrailPoint.x) && Number.isFinite(lastTrailPoint.y)) {
          const dx = px - lastTrailPoint.x;
          const dy = py - lastTrailPoint.y;
          const len = Math.hypot(dx, dy);
          if (len >= 1.5) {
            const seg = document.createElement('div');
            seg.style.position = 'fixed';
            seg.style.left = `${lastTrailPoint.x}px`;
            seg.style.top = `${lastTrailPoint.y}px`;
            seg.style.width = `${len}px`;
            seg.style.height = '4px';
            seg.style.transformOrigin = '0 50%';
            seg.style.transform = `rotate(${Math.atan2(dy, dx)}rad)`;
            seg.style.borderRadius = '999px';
            seg.style.background = 'rgba(0,180,255,1)';
            seg.style.boxShadow = '0 0 10px rgba(0,180,255,1)';
            seg.style.pointerEvents = 'none';
            seg.style.opacity = '0.95';
            seg.style.transition = 'opacity 5000ms linear';
            trailLayer.appendChild(seg);
            requestAnimationFrame(() => { seg.style.opacity = '0'; });
            setTimeout(() => seg.remove(), 5100);
          }
        }
        const dot = document.createElement('div');
        dot.style.position = 'fixed';
        dot.style.left = `${Math.max(0, px - 2.5)}px`;
        dot.style.top = `${Math.max(0, py - 2.5)}px`;
        dot.style.width = '7px';
        dot.style.height = '7px';
        dot.style.borderRadius = '50%';
        dot.style.background = 'rgba(0,180,255,1)';
        dot.style.pointerEvents = 'none';
        dot.style.opacity = '0.95';
        dot.style.transition = 'opacity 5000ms linear';
        trailLayer.appendChild(dot);
        requestAnimationFrame(() => { dot.style.opacity = '0'; });
        setTimeout(() => dot.remove(), 5100);
        lastTrailPoint = { x: px, y: py };
        };

        const normalizePoint = (x, y) => {
        const nx = Number(x);
        const ny = Number(y);
        if (!Number.isFinite(nx) || !Number.isFinite(ny)) return null;
        const w = window.innerWidth || 0;
        const h = window.innerHeight || 0;
        const cx = Math.max(0, w > 0 ? Math.min(w - 1, nx) : nx);
        const cy = Math.max(0, h > 0 ? Math.min(h - 1, ny) : ny);
        // Ignore noisy top-left synthetic points when we already have a stable cursor position.
        if (cx <= 1 && cy <= 1 && window.__bridgeCursorPos) {
          return { x: window.__bridgeCursorPos.x, y: window.__bridgeCursorPos.y };
        }
        return { x: cx, y: cy };
        };

        const setCursor = (x, y) => {
        const p = normalizePoint(x, y);
        if (!p) return;
        x = p.x;
        y = p.y;
        const normal = 14 * cfg.scale;
        window.__bridgeCursorPos = { x, y };
        cursor.style.width = `${normal}px`;
        cursor.style.height = `${normal}px`;
        cursor.style.left = `${Math.max(0, x - normal / 2)}px`;
        cursor.style.top = `${Math.max(0, y - normal / 2)}px`;
        };

        window.__bridgeGetCursorPos = () => {
        const pos = window.__bridgeCursorPos || null;
        if (!pos || typeof pos.x !== 'number' || typeof pos.y !== 'number') return null;
        return { x: pos.x, y: pos.y };
        };

        window.addEventListener('mousemove', (ev) => {
        if (!cfg.cursorEnabled) return;
        const st = window.__bridgeOverlayState || null;
        // Ignore native mousemove noise while assistant is driving the page.
        if (st && st.controlled) return;
        setCursor(ev.clientX, ev.clientY);
        emitTrail(ev.clientX, ev.clientY);
        }, true);

        window.__bridgeMoveCursor = (x, y) => {
        if (!cfg.cursorEnabled) return;
        const p = normalizePoint(x, y);
        if (!p) return;
        setCursor(p.x, p.y);
        emitTrail(p.x, p.y);
        };

        const initialPos = (() => {
          const prev = window.__bridgeCursorPos;
          if (prev && typeof prev.x === 'number' && typeof prev.y === 'number') {
            return { x: prev.x, y: prev.y };
          }
          const w = window.innerWidth || 0;
          const h = window.innerHeight || 0;
          return { x: Math.max(12, w * 0.5), y: Math.max(12, h * 0.5) };
        })();
        setCursor(initialPos.x, initialPos.y);

        window.__bridgeShowClick = (x, y, label) => {
        const p = normalizePoint(x, y);
        if (!p) return;
        x = p.x;
        y = p.y;
        if (cfg.cursorEnabled) {
          window.__bridgeMoveCursor(x, y);
        }
        if (cfg.clickPulseEnabled) {
          window.__bridgePulseAt(x, y);
        }
        if (label) {
          let badge = document.getElementById('__bridge_step_badge');
          if (!badge) {
            badge = document.createElement('div');
            badge.id = '__bridge_step_badge';
            badge.style.position = 'fixed';
            badge.style.zIndex = '2147483647';
            badge.style.padding = '4px 8px';
            badge.style.borderRadius = '6px';
            badge.style.font = '12px/1.2 monospace';
            badge.style.background = '#111';
            badge.style.color = '#fff';
            badge.style.pointerEvents = 'none';
            document.documentElement.appendChild(badge);
          }
          badge.textContent = label;
          badge.style.left = `${Math.max(0, x + 14)}px`;
          badge.style.top = `${Math.max(0, y - 8)}px`;
        }
        };

        window.__bridgePulseAt = (x, y) => {
        if (!cfg.clickPulseEnabled) return;
        const p = normalizePoint(x, y);
        if (!p) return;
        x = p.x;
        y = p.y;
        const normal = 14 * cfg.scale;
        const click = 22 * cfg.scale;
        if (cfg.cursorEnabled) {
          cursor.style.width = `${click}px`;
          cursor.style.height = `${click}px`;
          cursor.style.left = `${Math.max(0, x - click / 2)}px`;
          cursor.style.top = `${Math.max(0, y - click / 2)}px`;
          setTimeout(() => {
            cursor.style.width = `${normal}px`;
            cursor.style.height = `${normal}px`;
            cursor.style.left = `${Math.max(0, x - normal / 2)}px`;
            cursor.style.top = `${Math.max(0, y - normal / 2)}px`;
          }, 200);
        }
        const ring = document.createElement('div');
        ring.style.position = 'fixed';
        ring.style.left = `${Math.max(0, x - 10)}px`;
        ring.style.top = `${Math.max(0, y - 10)}px`;
        ring.style.width = '20px';
        ring.style.height = '20px';
        ring.style.borderRadius = '50%';
        ring.style.border = `2px solid ${cfg.color}`;
        ring.style.opacity = '0.9';
        ring.style.pointerEvents = 'none';
        ring.style.zIndex = '2147483647';
        ring.style.transform = 'scale(0.7)';
        ring.style.transition = 'transform 650ms ease, opacity 650ms ease';
        document.documentElement.appendChild(ring);
        requestAnimationFrame(() => {
          ring.style.transform = 'scale(2.1)';
          ring.style.opacity = '0';
        });
        setTimeout(() => ring.remove(), 720);
        };

        window.__bridgeDrawPath = (points) => {
        if (!cfg.cursorEnabled) return;
        if (!Array.isArray(points) || points.length < 2) return;
        const clean = points
          .map((p) => Array.isArray(p) ? { x: Number(p[0]), y: Number(p[1]) } : null)
          .filter((p) => p && Number.isFinite(p.x) && Number.isFinite(p.y));
        if (clean.length < 2) return;
        const svgNS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('width', '100%');
        svg.setAttribute('height', '100%');
        svg.setAttribute(
          'viewBox',
          `0 0 ${Math.max(1, window.innerWidth || 1)} ${Math.max(1, window.innerHeight || 1)}`
        );
        svg.style.position = 'fixed';
        svg.style.inset = '0';
        svg.style.pointerEvents = 'none';
        svg.style.zIndex = '2147483646';
        svg.style.overflow = 'visible';
        svg.style.opacity = '0.98';
        svg.style.transition = 'opacity 5000ms linear';
        const poly = document.createElementNS(svgNS, 'polyline');
        poly.setAttribute('fill', 'none');
        poly.setAttribute('stroke', 'rgba(0,180,255,1)');
        poly.setAttribute('stroke-width', '4');
        poly.setAttribute('stroke-linecap', 'round');
        poly.setAttribute('stroke-linejoin', 'round');
        poly.setAttribute('points', clean.map((p) => `${p.x},${p.y}`).join(' '));
        svg.appendChild(poly);
        trailLayer.appendChild(svg);
        requestAnimationFrame(() => { svg.style.opacity = '0'; });
        setTimeout(() => svg.remove(), 5100);
        };
        window.__bridgeResolveControlUrl = (state) => {
          const s = state || {};
          if (s.control_url && typeof s.control_url === 'string') return s.control_url;
          const p = Number(s.control_port || 0);
          if (p > 0) return `http://127.0.0.1:${p}`;
          return '';
        };
        window.__bridgeSetTopBarVisible = (visible) => {
          const bar = document.getElementById('__bridge_session_top_bar');
          if (!bar) return;
          if (visible) {
            bar.dataset.visible = '1';
            bar.style.transform = 'translateY(0)';
            bar.style.opacity = '1';
          } else {
            bar.dataset.visible = '0';
            bar.style.transform = 'translateY(-110%)';
            bar.style.opacity = '0';
          }
        };
        window.__bridgeSetIncidentOverlay = (enabled, message) => {
          const id = '__bridge_incident_overlay';
          const existing = document.getElementById(id);
          if (!enabled) {
            if (existing) existing.remove();
            return;
          }
          if (existing) {
            const badge = existing.querySelector('[data-role="badge"]');
            if (badge) badge.textContent = message || 'INCIDENT DETECTED';
            return;
          }
          const wrap = document.createElement('div');
          wrap.id = id;
          wrap.style.position = 'fixed';
          wrap.style.inset = '0';
          wrap.style.border = '3px solid #ff5252';
          wrap.style.boxSizing = 'border-box';
          wrap.style.pointerEvents = 'none';
          wrap.style.zIndex = '2147483645';
          const badge = document.createElement('div');
          badge.dataset.role = 'badge';
          badge.textContent = message || 'INCIDENT DETECTED';
          badge.style.position = 'fixed';
          badge.style.top = '10px';
          badge.style.left = '12px';
          badge.style.padding = '4px 8px';
          badge.style.borderRadius = '999px';
          badge.style.font = '11px/1.2 monospace';
          badge.style.color = '#fff';
          badge.style.background = 'rgba(255,82,82,0.92)';
          badge.style.pointerEvents = 'none';
          wrap.appendChild(badge);
          document.documentElement.appendChild(wrap);
        };
        window.__bridgeSendSessionEvent = (event) => {
          const bar = document.getElementById('__bridge_session_top_bar');
          const stateRaw = bar?.dataset?.state || '{}';
          let state;
          try { state = JSON.parse(stateRaw); } catch (_e) { state = {}; }
          const controlUrl = window.__bridgeResolveControlUrl(state);
          if (!controlUrl) return;
          const payload = {
            ...(event || {}),
            session_id: state.session_id || '',
            url: String((event && event.url) || location.href || ''),
            controlled: !!state.controlled,
            learning_active: !!state.learning_active,
            observer_noise_mode: String(state.observer_noise_mode || 'minimal'),
          };
          fetch(`${controlUrl}/event`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            keepalive: true,
          }).catch(() => null);
        };
        window.__bridgeEnsureSessionObserver = () => {
          if (window.__bridgeObserverInstalled) return;
          window.__bridgeObserverInstalled = true;
          let lastMoveTs = 0;
          let lastMoveX = 0;
          let lastMoveY = 0;
          let lastScrollTs = 0;
          let lastScrollY = 0;
          const shouldCapture = (eventType, bridgeControl = false) => {
            const bar = document.getElementById('__bridge_session_top_bar');
            const stateRaw = bar?.dataset?.state || '{}';
            let state = {};
            try { state = JSON.parse(stateRaw); } catch (_e) { state = {}; }
            const mode = String(state.observer_noise_mode || 'minimal').toLowerCase();
            if (mode === 'debug') return true;
            const controlled = !!state.controlled;
            const learningActive = !!state.learning_active;
            if (eventType === 'click') {
              if (bridgeControl) return false;
              return controlled || learningActive;
            }
            if (eventType === 'scroll') {
              return learningActive;
            }
            if (eventType === 'mousemove') {
              return false;
            }
            return true;
          };
          const cssPath = (node) => {
            try {
              if (!node || !(node instanceof Element)) return '';
              if (node.id) return `#${node.id}`;
              const testid = node.getAttribute && (node.getAttribute('data-testid') || node.getAttribute('data-test'));
              if (testid) return `[data-testid="${testid}"]`;
              const tag = String(node.tagName || '').toLowerCase();
              const cls = String(node.className || '').trim().split(/\\s+/).filter(Boolean).slice(0, 2).join('.');
              if (tag) return cls ? `${tag}.${cls}` : tag;
              return '';
            } catch (_e) { return ''; }
          };
          document.addEventListener('click', (ev) => {
            const el = ev.target;
            let target = '';
            let selector = '';
            let text = '';
            let bridgeControl = false;
            let controlled = false;
            try {
              const bar = document.getElementById('__bridge_session_top_bar');
              const raw = bar?.dataset?.state || '{}';
              const state = JSON.parse(raw);
              controlled = !!state.controlled;
            } catch (_e) { controlled = false; }
            if (el && typeof el.closest === 'function') {
              const btn = el.closest('button,[role="button"],a,input,select,textarea');
              if (btn) {
                target = (btn.textContent || btn.id || btn.className || '').trim();
                selector = cssPath(btn);
                text = String(btn.textContent || '').trim().slice(0, 180);
                const bid = String(btn.id || '');
                bridgeControl = bid.startsWith('__bridge_') || selector.includes('__bridge_');
              }
            }
            if (!bridgeControl && !controlled && shouldCapture('click', bridgeControl)) {
              window.__bridgeShowClick?.(
                Number(ev.clientX || 0),
                Number(ev.clientY || 0),
                'manual click captured'
              );
            }
            if (!shouldCapture('click', bridgeControl)) return;
            window.__bridgeSendSessionEvent({
              type: 'click',
              target,
              selector,
              text,
              message: `click ${target}`,
              x: Number(ev.clientX || 0),
              y: Number(ev.clientY || 0),
            });
          }, true);
          window.addEventListener('mousemove', (ev) => {
            if (!shouldCapture('mousemove', false)) return;
            const now = Date.now();
            if ((now - lastMoveTs) < 350) return;
            const x = Number(ev.clientX || 0);
            const y = Number(ev.clientY || 0);
            const dist = Math.hypot(x - lastMoveX, y - lastMoveY);
            if (dist < 18) return;
            lastMoveTs = now;
            lastMoveX = x;
            lastMoveY = y;
            window.__bridgeSendSessionEvent({
              type: 'mousemove',
              message: `mousemove ${x},${y}`,
              x,
              y,
            });
          }, true);
          window.addEventListener('scroll', () => {
            if (!shouldCapture('scroll', false)) return;
            const now = Date.now();
            if ((now - lastScrollTs) < 300) return;
            const sy = Number(window.scrollY || window.pageYOffset || 0);
            const delta = Math.abs(sy - lastScrollY);
            if (delta < 80) return;
            lastScrollTs = now;
            lastScrollY = sy;
            window.__bridgeSendSessionEvent({
              type: 'scroll',
              message: `scroll y=${sy}`,
              scroll_y: sy,
            });
          }, { passive: true, capture: true });
          window.addEventListener('error', (ev) => {
            window.__bridgeSendSessionEvent({
              type: 'page_error',
              message: String(ev.message || 'window error'),
            });
          });
          window.addEventListener('unhandledrejection', (ev) => {
            window.__bridgeSendSessionEvent({
              type: 'page_error',
              message: String(ev.reason || 'unhandled rejection'),
            });
          });
          if (!window.__bridgeFetchWrapped && typeof window.fetch === 'function') {
            window.__bridgeFetchWrapped = true;
            const origFetch = window.fetch.bind(window);
            window.fetch = async (...args) => {
              try {
                const resp = await origFetch(...args);
                if (resp && Number(resp.status || 0) >= 400) {
                  window.__bridgeSendSessionEvent({
                    type: Number(resp.status || 0) >= 500 ? 'network_error' : 'network_warn',
                    status: Number(resp.status || 0),
                    url: String(resp.url || args[0] || ''),
                    message: `http ${resp.status}`,
                  });
                }
                return resp;
              } catch (err) {
                window.__bridgeSendSessionEvent({
                  type: 'network_error',
                  status: 0,
                  url: String(args[0] || ''),
                  message: String(err || 'fetch failed'),
                });
                throw err;
              }
            };
          }
          if (!window.__bridgeXhrWrapped && window.XMLHttpRequest) {
            window.__bridgeXhrWrapped = true;
            const origOpen = XMLHttpRequest.prototype.open;
            const origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method, url, ...rest) {
              this.__bridgeMethod = String(method || 'GET');
              this.__bridgeUrl = String(url || '');
              return origOpen.call(this, method, url, ...rest);
            };
            XMLHttpRequest.prototype.send = function(...args) {
              this.addEventListener('loadend', () => {
                const st = Number(this.status || 0);
                if (st >= 400 || st === 0) {
                  window.__bridgeSendSessionEvent({
                    type: (st === 0 || st >= 500) ? 'network_error' : 'network_warn',
                    status: st,
                    url: String(this.responseURL || this.__bridgeUrl || ''),
                    message: `xhr ${st}`,
                  });
                }
              });
              return origSend.apply(this, args);
            };
          }
        };
        window.__bridgeStartTopBarPolling = (state) => {
          const controlUrl = window.__bridgeResolveControlUrl(state || {});
          if (window.__bridgeTopBarPollTimer) {
            clearInterval(window.__bridgeTopBarPollTimer);
            window.__bridgeTopBarPollTimer = null;
          }
          if (!controlUrl) return;
          window.__bridgeTopBarPollTimer = setInterval(async () => {
            try {
              const resp = await fetch(`${controlUrl}/state`, { cache: 'no-store' });
              const payload = await resp.json();
              if (resp.ok && payload && typeof payload === 'object') {
                window.__bridgeUpdateTopBarState(payload);
              }
            } catch (_err) {
              // keep previous state; button actions will surface offline errors.
            }
          }, 2500);
        };
        window.__bridgeControlRequest = async (action) => {
          const bar = document.getElementById('__bridge_session_top_bar');
          const stateRaw = bar?.dataset?.state || '{}';
          let state;
          try { state = JSON.parse(stateRaw); } catch (_e) { state = {}; }
          const controlUrl = window.__bridgeResolveControlUrl(state);
          if (!controlUrl) {
            return { ok: false, error: 'agent offline' };
          }
          try {
            const resp = await fetch(`${controlUrl}/action`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ action }),
            });
            let payload = {};
            try { payload = await resp.json(); } catch (_e) { payload = {}; }
            if (!resp.ok) {
              const msg = payload.error || `http ${resp.status}`;
              return { ok: false, error: String(msg), payload };
            }
            return { ok: true, payload };
          } catch (err) {
            return { ok: false, error: String(err || 'agent offline') };
          }
        };
        window.__bridgeEnsureTopBar = (state) => {
          const id = '__bridge_session_top_bar';
          let bar = document.getElementById(id);
          if (!bar) {
            bar = document.createElement('div');
            bar.id = id;
            bar.style.position = 'fixed';
            bar.style.top = '0';
            bar.style.left = '0';
            bar.style.right = '0';
            bar.style.height = '42px';
            bar.style.display = 'flex';
            bar.style.alignItems = 'center';
            bar.style.gap = '10px';
            bar.style.padding = '6px 10px';
            bar.style.font = '12px/1.2 monospace';
            bar.style.zIndex = '2147483644';
            bar.style.pointerEvents = 'auto';
            bar.style.backdropFilter = 'blur(4px)';
            bar.style.borderBottom = '1px solid rgba(255,255,255,0.18)';
            bar.style.transform = 'translateY(-110%)';
            bar.style.opacity = '0';
            bar.style.transition = 'transform 210ms ease-out, opacity 210ms ease-out';
            bar.dataset.visible = '0';
            const hot = document.createElement('div');
            hot.id = '__bridge_top_hot';
            hot.style.position = 'fixed';
            hot.style.top = '0';
            hot.style.left = '0';
            hot.style.right = '0';
            hot.style.height = '8px';
            hot.style.pointerEvents = 'auto';
            hot.style.zIndex = '2147483643';
            hot.addEventListener('mouseenter', () => window.__bridgeSetTopBarVisible(true));
            bar.addEventListener('mouseleave', () => window.__bridgeSetTopBarVisible(false));
            const toggle = document.createElement('button');
            toggle.id = '__bridge_top_toggle';
            toggle.textContent = '◉';
            toggle.style.position = 'fixed';
            toggle.style.top = '6px';
            toggle.style.left = '6px';
            toggle.style.zIndex = '2147483644';
            toggle.style.width = '18px';
            toggle.style.height = '18px';
            toggle.style.padding = '0';
            toggle.style.font = '12px monospace';
            toggle.style.borderRadius = '999px';
            toggle.style.border = '1px solid rgba(255,255,255,0.35)';
            toggle.style.background = 'rgba(17,17,17,0.65)';
            toggle.style.color = '#fff';
            toggle.style.pointerEvents = 'auto';
            toggle.addEventListener('click', () => {
              window.__bridgeSetTopBarVisible(bar.dataset.visible !== '1');
            });
            overlayHost.appendChild(hot);
            overlayHost.appendChild(toggle);
            overlayHost.appendChild(bar);
          }
          window.__bridgeUpdateTopBarState(state);
        };
        window.__bridgeUpdateTopBarState = (state) => {
          const bar = document.getElementById('__bridge_session_top_bar');
          if (!bar) return;
          const s = state || {};
          const controlled = !!s.controlled;
          const open = String(s.state || 'open') === 'open';
          const controlUrl = window.__bridgeResolveControlUrl(s);
          const agentOnline = !!controlUrl && s.agent_online !== false;
          const incidentOpen = !!s.incident_open;
          const learningActive = !!s.learning_active;
          const readyManual = open && !controlled && agentOnline && !incidentOpen && !learningActive;
          const incidentText = String(s.last_error || '').slice(0, 96);
          bar.style.background = controlled
            ? 'rgba(59,167,255,0.22)'
            : (
              incidentOpen
                ? 'rgba(255,82,82,0.26)'
                : (
                  learningActive
                    ? 'rgba(245,158,11,0.24)'
                    : (
                      readyManual
                    ? 'rgba(22,163,74,0.22)'
                    : (open ? 'rgba(80,80,80,0.28)' : 'rgba(20,20,20,0.7)')
                    )
                )
            );
          bar.style.borderBottom = learningActive
            ? '2px solid rgba(245,158,11,0.95)'
            : (
              readyManual
                ? '2px solid rgba(34,197,94,0.95)'
                : '1px solid rgba(255,255,255,0.18)'
            );
          bar.dataset.state = JSON.stringify(s);
          window.__bridgeSetIncidentOverlay(incidentOpen && !controlled, incidentText || 'INCIDENT DETECTED');
          window.__bridgeSetStateBorder?.(s);
          window.__bridgeEnsureSessionObserver();
          window.__bridgeStartTopBarPolling(s);
          const ctrl = controlled
            ? 'ASSISTANT CONTROL'
            : (learningActive ? 'LEARNING/HANDOFF' : 'USER CONTROL');
          const url = String(s.url || '').slice(0, 70);
          const last = String(s.last_seen_at || '').replace('T', ' ').slice(0, 16);
          const status = !agentOnline
            ? 'agent offline'
            : (
              incidentOpen
                ? `incident open (${Number(s.error_count || 0)})`
                : ''
            );
          const readyBadge = readyManual
            ? `<span
                 id=\"__bridge_ready_badge\"
                 aria-label=\"session-ready-manual-test\"
                 style=\"
                   display:inline-flex;
                   align-items:center;
                   gap:6px;
                   background:#16a34a;
                   color:#fff;
                   border:1px solid #22c55e;
                   font-size:13px;
                   font-weight:700;
                   padding:6px 10px;
                   border-radius:999px;\"
               >● READY FOR MANUAL TEST</span>`
            : '';
          bar.innerHTML = `
            <strong>session ${s.session_id || '-'}</strong>
            <span>state:${s.state || '-'}</span>
            <span>control:${ctrl}</span>
            <span>url:${url}</span>
            <span>seen:${last}</span>
            ${readyBadge}
            <span id=\"__bridge_status_msg\" style=\"color:${agentOnline ? '#b7d8ff' : '#ffb3b3'}\">${status}</span>
            <button
              id=\"__bridge_ack_btn\" ${(open && agentOnline && incidentOpen) ? '' : 'disabled'}
            >Clear incident</button>
            <button id=\"__bridge_release_btn\" ${(open && agentOnline) ? '' : 'disabled'}>Release</button>
            <button id=\"__bridge_close_btn\" ${(open && agentOnline) ? '' : 'disabled'}>Close</button>
            <button id=\"__bridge_refresh_btn\" ${agentOnline ? '' : 'disabled'}>Refresh</button>
          `;
          const statusEl = bar.querySelector('#__bridge_status_msg');
          const ackBtn = bar.querySelector('#__bridge_ack_btn');
          const release = bar.querySelector('#__bridge_release_btn');
          const closeBtn = bar.querySelector('#__bridge_close_btn');
          const refresh = bar.querySelector('#__bridge_refresh_btn');
          const wire = (btn, action) => {
            if (!btn) return;
            btn.onclick = async () => {
              btn.disabled = true;
              if (statusEl) statusEl.textContent = `${action}...`;
              const result = await window.__bridgeControlRequest(action);
              if (!result.ok) {
                if (statusEl) statusEl.textContent = result.error || 'action failed';
                window.__bridgeUpdateTopBarState({ ...s, agent_online: false });
                return;
              }
              if (statusEl) statusEl.textContent = 'ok';
              window.__bridgeUpdateTopBarState(result.payload || s);
            };
          };
          wire(ackBtn, 'ack');
          wire(release, 'release');
          wire(closeBtn, 'close');
          wire(refresh, 'refresh');
        };
        window.__bridgeDestroyTopBar = () => {
          document.getElementById('__bridge_session_top_bar')?.remove();
          document.getElementById('__bridge_top_hot')?.remove();
          document.getElementById('__bridge_top_toggle')?.remove();
          window.__bridgeSetIncidentOverlay(false);
          if (window.__bridgeTopBarPollTimer) {
            clearInterval(window.__bridgeTopBarPollTimer);
            window.__bridgeTopBarPollTimer = null;
          }
        };
        if (sessionState && sessionState.session_id) {
          window.__bridgeEnsureTopBar(sessionState);
        }
        window.__bridgeOverlayInstalled = true;
        return true;
      };

      window.__bridgeEnsureOverlay = () => installOverlay();
      installOverlay();
    })();
    """
    script = script_template.replace("__CFG_JSON__", json.dumps(config, ensure_ascii=False))
    script = script.replace("__SESSION_JSON__", session_json)
    page.add_init_script(script)
    # Also execute on current page for attach/reuse flows where no navigation occurs.
    try:
        page.evaluate(script)
    except Exception:
        pass


def _highlight_target(
    page: Any,
    locator: Any,
    label: str,
    *,
    click_pulse_enabled: bool,
    show_preview: bool = True,
    auto_scroll: bool = True,
) -> tuple[float, float] | None:
    last_exc: Exception | None = None
    for _ in range(4):
        try:
            if auto_scroll:
                try:
                    locator.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    locator.evaluate("el => el.scrollIntoView({block:'center', inline:'center'})")
                except Exception:
                    pass

            info = locator.evaluate(
                """
                (el) => {
                  const r = el.getBoundingClientRect();
                  const x = r.left + (r.width / 2);
                  const y = r.top + (r.height / 2);
                  const inViewport = (
                    x >= 0 && y >= 0 &&
                    x <= window.innerWidth && y <= window.innerHeight &&
                    r.width > 0 && r.height > 0
                  );
                  const top = inViewport ? document.elementFromPoint(x, y) : null;
                  const ok = !!top && (top === el || (el.contains && el.contains(top)));
                  return { x, y, ok };
                }
                """
            )
            if isinstance(info, dict) and bool(info.get("ok", False)):
                x = float(info.get("x", 0.0))
                y = float(info.get("y", 0.0))
                if show_preview:
                    page.evaluate(
                        "([x, y, label]) => window.__bridgeShowClick?.(x, y, label)",
                        [x, y, label],
                    )
                if show_preview and click_pulse_enabled:
                    page.evaluate("([x, y]) => window.__bridgePulseAt?.(x, y)", [x, y])
                page.wait_for_timeout(120)
                return (x, y)

            if auto_scroll:
                # Likely occluded by fixed UI (e.g., dock). Scroll up a bit and retry.
                try:
                    page.evaluate("() => window.scrollBy(0, -120)")
                except Exception:
                    pass
                try:
                    page.wait_for_timeout(60)
                except Exception:
                    pass
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc is not None:
        return None
    return None


def _ensure_visual_overlay_installed(page: Any) -> None:
    try:
        page.evaluate("() => window.__bridgeEnsureOverlay?.()")
    except Exception:
        return


def _verify_visual_overlay_visible(page: Any) -> None:
    snapshot = _read_visual_overlay_snapshot(page)
    try:
        opacity = float(str(snapshot.get("opacity", "0") or "0"))
    except Exception:
        opacity = 0.0
    z_index = int(snapshot.get("z_index", 0) or 0)
    ok = bool(
        snapshot.get("exists")
        and snapshot.get("parent") == "body"
        and snapshot.get("display") != "none"
        and snapshot.get("visibility") != "hidden"
        and opacity > 0
        and z_index >= 2147483647
        and snapshot.get("pointer_events") == "none"
    )
    if not ok:
        raise RuntimeError(
            "Visual overlay not visible: missing #__bridge_cursor_overlay or invalid style."
        )


def _read_visual_overlay_snapshot(page: Any) -> dict[str, Any]:
    try:
        raw = page.evaluate(
            """
            () => {
              const el = document.getElementById('__bridge_cursor_overlay');
              if (!el) return { exists: false };
              const style = window.getComputedStyle(el);
              const parent = el.parentElement && el.parentElement.tagName
                ? el.parentElement.tagName.toLowerCase()
                : '';
              const z = Number.parseInt(style.zIndex || '0', 10);
              return {
                exists: true,
                parent,
                display: style.display || '',
                visibility: style.visibility || '',
                opacity: style.opacity || '0',
                z_index: Number.isNaN(z) ? 0 : z,
                pointer_events: style.pointerEvents || '',
              };
            }
            """
        )
    except Exception as exc:
        return {"exists": False, "error": str(exc)}
    if isinstance(raw, dict):
        return raw
    return {"exists": False, "error": "overlay snapshot is not a dict"}
