/* InferFabric Dashboard — State Store
 * Phase: P0 | Task: T.0-2
 * Pub/sub state management with 3s API polling.
 * Fetches /api/status + /system, merges into normalized shape,
 * triggers bindings.render() on state change.
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

  /* ── API polling ── */
async fetchStatus() {
    try {
      const [statusRes, sysRes] = await Promise.all([
        fetch('/status'),
        fetch('/system').catch(() => null)
      ]);
      const status = await statusRes.json();
      const sys = sysRes ? await sysRes.json().catch(() => ({})) : {};

      // Check for API errors
      if (status.error) {
        this.set('api_error', status.error);
        return;
      }
      this.set('api_error', null);

      // Normalize into binding-friendly shape
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
        temp: sys.gpu_temp_c || null,
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

      // Trigger render via batched macrotask
      this._scheduleRender();
    } catch(e) {
      console.warn('[state] fetch error:', e);
      this.set('api_error', e.message);
    }
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
    this.fetchStatus();
    // Clear any existing timer
    if (this._timer) clearInterval(this._timer);
    // Start new timer
    this._timer = setInterval(() => this.fetchStatus(), intervalMs);
  }

  stopPolling() {
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
  }

  /* ── Tab Management ── */
  switchTab(tabId) {
    if (this._switchLocked) return;
    // Deactivate all tabs
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.snav-item').forEach(t => t.classList.remove('active'));
    // Activate target
    const target = document.getElementById(tabId);
    if (target) target.classList.add('active');
    const btn = document.querySelector(`.snav-item[data-tab="${tabId}"]`);
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
    document.querySelectorAll('.snav-item').forEach(el => el.classList.add('locked'));
    store.setSwitchLocked(true);
  } else {
    overlay.style.display = 'none';
    document.querySelectorAll('.snav-item').forEach(el => el.classList.remove('locked'));
    store.setSwitchLocked(false);
  }
});

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