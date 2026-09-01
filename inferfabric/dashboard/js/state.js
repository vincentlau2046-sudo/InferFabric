/* InferFabric Dashboard — State Store
 * Phase: P0 | Task: T.0-2 (v2: /api/snapshot upgrade)
 * Single-source polling: fetches /api/snapshot (3s), merges into a
 * normalized shape, and triggers bindings.render() on state change.
 *
 * Change detection: the endpoint supports ETag/If-None-Match → 304 when
 * the control plane (status + models) is unchanged. The store therefore
 * keeps its last known state on 304 — this eliminates the "state gap"
 * where stale poll responses used to overwrite fresh data.
 *
 * Exposed as window.store for app.js integration.
 */

class StateStore {
  constructor(initial = {}) {
    this._state = { ...initial };
    this._subs = {};
    this._timer = null;
    this._tabActive = null;
    this._switchLocked = false;
    // Snapshot sync metadata
    this._etag = null;   // last etag received from /api/snapshot
    this._lastTs = 0;    // last (monotonic) snapshot timestamp
  }

  /* ── Core pub/sub ── */
  get(key) { return key ? this._state[key] : this._state; }

  set(key, value) {
    const old = this._state[key];
    if (old === value) return;  // skip no-op
    this._state[key] = value;
    this._notify(key, value, old);
  }

  update(obj, prefix = '') {
    if (obj == null || typeof obj !== 'object') return;
    for (const [k, v] of Object.entries(obj)) {
      const fullKey = prefix ? `${prefix}.${k}` : k;
      if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
        // Recurse into nested objects
        const existing = this._state[fullKey] || {};
        if (existing !== v) {
          this._state[fullKey] = { ...existing, ...v };
          this._notify(fullKey, this._state[fullKey], existing);
        }
      } else {
        const oldVal = this._state[fullKey];
        if (oldVal !== v) {
          this._state[fullKey] = v;
          this._notify(fullKey, v, oldVal);
        }
      }
    }
  }

  on(key, callback) {
    if (!this._subs[key]) this._subs[key] = new Set();
    this._subs[key].add(callback);
    const v = this._state[key];
    if (v !== undefined) callback(v, undefined);
    return () => this._subs[key].delete(callback);
  }

  _notify(key, value, old) {
    const subs = this._subs[key];
    if (subs) subs.forEach(cb => { try { cb(value, old); } catch(e) { console.warn('[state] sub error:', key, e); } });
  }

  /* ── API polling (single source of truth: /api/snapshot) ── */
  async fetchSnapshot(force = false) {
    try {
      const headers = { cache: 'no-store' };
      if (this._etag && !force) headers['If-None-Match'] = this._etag;
      const res = await fetch('/api/snapshot', { headers });

      if (res.status === 304) {
        // Control plane unchanged — keep last state, update sync meta only.
        this.set('api_error', null);
        this.set('sync_meta', {
          etag: this._etag,
          rev: (this._etag || '').slice(1, 9) || '',
          ts: this._lastTs,
          changed: false,
          stale: false,
        });
        return null;
      }
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const snap = await res.json();

      // Out-of-order guard: drop stale responses (ts regressed).
      const stale = !force && snap.ts != null && this._lastTs > 0 && snap.ts <= this._lastTs;
      this._etag = snap.etag || null;
      this._lastTs = Math.max(this._lastTs, snap.ts || 0);
      this.set('api_error', null);

      if (stale) {
        console.warn('[state] out-of-order snapshot dropped (ts', snap.ts, '<= last', this._lastTs);
        this.set('sync_meta', {
          etag: this._etag,
          rev: (this._etag || '').slice(1, 9) || '',
          ts: this._lastTs,
          changed: false,
          stale: true,
        });
        return snap;
      }

      const status = snap.status || {};
      const sys = snap.system || {};

      // ── Normalize into binding-friendly shape (merged via update → merge)
      const gpuUsed = status.gpu_used_mb || 0;
      const gpuTotal = status.gpu_total_mb || 32607;
      const ramTotal = sys.ram_total_gb || 1;
      const ramUsed = sys.ram_used_gb || 0;

      const merged = {
        gpu_mode: status.gpu_mode || 'idle',
        gpu: {
          used:  gpuUsed,
          total: gpuTotal,
          pct:   (gpuUsed / gpuTotal * 100),
        },
        gpu_util: {
          pct:   sys.gpu_util_pct || 0,
          clock: sys.gpu_clock_mhz || '—',
          power: sys.gpu_power_w || '—',
          temp:  sys.gpu_temp_c || null,
        },
        mem: {
          used:  ramUsed,
          total: ramTotal,
          pct:   (ramUsed / ramTotal * 100),
        },
        cpu: {
          pct:    sys.cpu_percent || 0,
          cores:  sys.cpu_cores || '—',
          uptime: sys.uptime_seconds || 0,
        },
        version: status.version || sys.version || '',
        switch_target: status.switch_target || null,
        // Raw passthrough for app.js consumers
        active_services: status.active_services || [],
        services_info:   status.services_info || {},
        services_health: status.services_health || {},
        sleep_states:    status.sleep_states || {},
      };
      this.update(merged);

      // ── Replace semantics for collections/maps: drop stale entries ──
      this.set('status', status);
      this.set('active_services', merged.active_services);
      this.set('services_info', merged.services_info);
      this.set('services_health', merged.services_health);
      this.set('sleep_states', merged.sleep_states);
      this.set('models', snap.models || []);
      this.set('history', snap.history || []);
      this.set('token_stats', snap.token_stats || {});
      this.set('request_log', snap.request_log || []);
      this.set('metrics_24h', snap.metrics_24h || {});
      this.set('local_models', snap.local_models || { discovered: [], configured: [] });
      this.set('sync_meta', {
        etag: this._etag,
        rev: (this._etag || '').slice(1, 9) || '',
        ts: snap.ts || this._lastTs,
        changed: true,
        stale: false,
      });

      // Trigger render via batched macrotask
      this._scheduleRender();
      return snap;
    } catch (e) {
      console.warn('[state] snapshot fetch error:', e);
      this.set('api_error', e.message);
      this.set('sync_meta', {
        etag: this._etag,
        rev: (this._etag || '').slice(1, 9) || '',
        ts: this._lastTs,
        changed: false,
        stale: true,
      });
      return null;
    }
  }

  /* Force a fresh snapshot (manual refresh / post-action refresh). */
  async forceRefresh() {
    const snap = await this.fetchSnapshot(true);
    if (snap && typeof window.refreshPanels === 'function') {
      try { window.refreshPanels(snap); } catch (e) { console.warn('[state] refreshPanels error:', e); }
    }
    return snap;
  }

  _scheduleRender() {
    if (this._renderScheduled) return;
    this._renderScheduled = true;
    // Use macrotask to batch multiple updates into one render
    setTimeout(() => {
      this._renderScheduled = false;
      if (window.render) window.render(this._state);
    }, 0);
  }

  startPolling(intervalMs = 3000) {
    // Immediate fetch
    this.fetchSnapshot();
    // Clear any existing timer
    if (this._timer) clearInterval(this._timer);
    // Start new timer
    this._timer = setInterval(() => this.fetchSnapshot(), intervalMs);
  }

  stopPolling() {
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
  }

  /* ── Tab Management ── */
  switchTab(tabId) {
    if (this._switchLocked) return;
    // Deactivate all tabs
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.if-nav-item').forEach(t => t.classList.remove('active'));
    // Activate target
    const target = document.getElementById(tabId);
    if (target) target.classList.add('active');
    const btn = document.querySelector(`.if-nav-item[data-tab="${tabId}"]`);
    if (btn) btn.classList.add('active');
    // Store + notify
    this._tabActive = tabId;
    this.set('tab_active', tabId);
    // Save to localStorage
    try { localStorage.setItem('iff_active_tab', tabId); } catch(e) {}
  }

  restoreTab() {
    try {
      const saved = localStorage.getItem('iff_active_tab');
      if (saved) this.switchTab(saved);
    } catch(e) {}
  }

  /* ── Switch Lock ── */
  setSwitchLocked(locked) {
    this._switchLocked = locked;
  }
  isSwitchLocked() {
    return this._switchLocked;
  }
}

/* ── Bootstrap ─────────────────────────────── */
const store = new StateStore();
window.store = store;
window.startPolling = (ms) => store.startPolling(ms);
window.restoreTab = () => store.restoreTab();

// Render on state changes (batched)
store.on('gpu_mode', () => store._scheduleRender());

// Expose switchTab globally (replaces old app.js version)
window.switchTab = (tabId) => store.switchTab(tabId);

/* ── Switch Overlay (P5.4) ── */
store.on('switch_target', (val) => {
  const overlay = document.getElementById('switchOverlay');
  if (!overlay) return;
  if (val) {
    overlay.style.display = '';
    document.getElementById('switchOverlayMsg').textContent = `正在切换到 ${val}…`;
    // Lock sidebar nav
    document.querySelectorAll('.if-nav-item').forEach(el => el.classList.add('locked'));
    store.setSwitchLocked(true);
  } else {
    overlay.style.display = 'none';
    document.querySelectorAll('.if-nav-item').forEach(el => el.classList.remove('locked'));
    store.setSwitchLocked(false);
  }
});

/* ── Sync / Freshness Indicator ── */
function updateSyncIndicator(meta) {
  if (!meta) return;
  const dot = document.getElementById('sidebarStatusDot');
  const txt = document.getElementById('sidebarSyncTxt');
  const stale = meta.stale || (!meta.changed && ((Date.now() / 1000) - (meta.ts || 0) > 6));
  if (dot) {
    dot.className = 'sidebar-status-dot ' + (stale ? 'err' : 'ok');
  }
  if (txt) {
    txt.textContent = stale ? '已断线' : (meta.rev ? '已同步 · rev ' + meta.rev : '运行中');
  }
  // Note: top-bar #syncMeta text is owned by the sync_meta binding in bindings.js
}
store.on('sync_meta', updateSyncIndicator);

/* ── Theme Toggle (P4) ── */
function toggleTheme() {
  const html = document.documentElement;
  const isDark = html.getAttribute('data-theme') === 'dark';
  if (isDark) {
    html.removeAttribute('data-theme');
    localStorage.setItem('iff_theme', 'light');
  } else {
    html.setAttribute('data-theme', 'dark');
    localStorage.setItem('iff_theme', 'dark');
  }
  updateThemeIcon();
}

function updateThemeIcon() {
  const btn = document.getElementById('themeToggle');
  if (!btn) return;
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  btn.textContent = isDark ? '🌙' : '☀️';
}

// Initialize theme from localStorage or system preference
(function initTheme() {
  const saved = localStorage.getItem('iff_theme');
  if (saved === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
  } else if (saved === 'light') {
    document.documentElement.removeAttribute('data-theme');
  } else if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
    document.documentElement.setAttribute('data-theme', 'dark');
  }
  updateThemeIcon();
})();
