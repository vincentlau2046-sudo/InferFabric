/* InferFabric Dashboard — Data Binding Layer
 * Phase: P0 | Task: T.0-1
 * Converts scattered getElementById() calls into a single binding table.
 *
 * BINDINGS: key → {el, type, fmt, prop, condition}
 * render(data)     — iterate BINDINGS → apply each
 * applyBinding(b)  — dispatch by type: text/style/class/visible/toast
 * getNested(obj, k)  — "gpu.used" → obj.gpu.used
 * showToast(msg, sev) — create/dismiss toast notification
 */

/* ── Binding Table ────────────────────────────────
 * Maps API response fields to DOM elements.
 * Element IDs match existing HTML (gP, gB, rP, cP, etc.)
 *
 * Data shape (merged from /api/status + /system):
 * {
 *   gpu_mode: "exclusive",
 *   gpu: { used: 29800, total: 32607, pct: 91.4 },        // from /status
 *   gpu_util: { pct: 45.2, clock: 2100, power: 120.5 },   // from /system
 *   mem:  { used: 5.8, total: 8.0, pct: 72.5 },            // from /system (GB)
 *   cpu:  { pct: 12.3, cores: 8, uptime: 3600 },           // from /system
 *   version: "4.7.0",
 *   switch_target: null,
 *   api_error: null
 * }
 */
const BINDINGS = [
  // ── GPU Mode Tag ──
  { key: 'gpu_mode',  el: 'sTxt',  type: 'text',
    fmt: v => ({ idle:'idle', exclusive:'exclusive', shared:'shared' }[v] || v) },
  { key: 'gpu_mode',  el: 'sTag',  type: 'class',  prefix: 'tag ' },

  // ── API Error Banner ──
  { key: 'api_error', el: 'apiErrorBanner', type: 'visible' },

  // ── GPU Memory (from /status) ──
  { key: 'gpu.pct',   el: 'gP',  type: 'text',  fmt: v => v.toFixed(1) },
  { key: 'gpu.pct',   el: 'gB',  type: 'style', prop: 'width',  fmt: v => v.toFixed(1)+'%' },
  { key: 'gpu.pct',   el: 'gB',  type: 'style', prop: 'background',
    fmt: v => v < 50 ? 'var(--blue)' : v < 80 ? 'var(--orange)' : 'var(--red)' },
  { key: 'gpu.used',  el: 'gU',  type: 'text',  fmt: v => v.toLocaleString() },
  { key: 'gpu.total', el: 'gT',  type: 'text',  fmt: v => v.toLocaleString() },

  // ── GPU Utilization (from /system) ──
  { key: 'gpu_util.pct',   el: 'guP', type: 'text',  fmt: v => v.toFixed(1) },
  { key: 'gpu_util.pct',   el: 'guB', type: 'style', prop: 'width',
    fmt: v => v.toFixed(1)+'%' },
  { key: 'gpu_util.pct',   el: 'guB', type: 'style', prop: 'background',
    fmt: v => v < 30 ? 'var(--green)' : v < 70 ? 'var(--orange)' : 'var(--red)' },
  { key: 'gpu_util.clock', el: 'guC', type: 'text' },
  { key: 'gpu_util.power', el: 'guW', type: 'text' },
  { key: 'gpu_util.temp',  el: 'guT', type: 'text',  fmt: v => v != null ? v + "°C" : "—" },

  // ── RAM (from /system) ──
  { key: 'mem.pct',   el: 'rP', type: 'text',  fmt: v => v.toFixed(1) },
  { key: 'mem.pct',   el: 'rB', type: 'style', prop: 'width',  fmt: v => v.toFixed(1)+'%' },
  { key: 'mem.used',  el: 'rU', type: 'text',  fmt: v => v.toFixed(1) },
  { key: 'mem.total', el: 'rT', type: 'text',  fmt: v => v.toFixed(1) },

  // ── CPU (from /system) ──
  { key: 'cpu.pct',    el: 'cP', type: 'text',  fmt: v => v.toFixed(1) },
  { key: 'cpu.pct',    el: 'cB', type: 'style', prop: 'width',  fmt: v => v.toFixed(1)+'%' },
  { key: 'cpu.cores',  el: 'cC', type: 'text' },
  { key: 'cpu.uptime', el: 'cU', type: 'text',
    fmt: v => Math.floor(v/3600)+'h '+Math.floor((v%3600)/60)+'m' },

  // ── Version / Timestamp ──
  { key: 'version', el: 'navVer', type: 'text', fmt: v => 'v'+v },
  { key: 'ts_now',  el: 'ts',    type: 'text',
    fmt: () => new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) },

  // ── v5.x: Sync / Freshness (top bar) ──
  { key: 'sync_meta', el: 'syncMeta', type: 'text',
    fmt: function(m) {
      if (!m) return '';
      var t = m.ts ? new Date(m.ts * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', second:'2-digit'}) : '—';
      return m.stale ? '已断线' : '已同步 ' + t + ' · rev ' + (m.rev || '—');
    } },

  // ── Toast — switch notifications ──
  { key: 'switch_target', el: 'toast', type: 'toast',
    condition: v => v !== null && v !== '' && v !== undefined,
    msg:      v => `正在切换到 ${v}...`,
    severity: 'info' },
];

/* ── Helpers ──────────────────────────────────── */

function getNested(obj, path) {
  return path.split('.').reduce((acc, p) => (acc != null ? acc[p] : undefined), obj);
}

function applyBinding(binding, value) {
  const el = document.getElementById(binding.el);
  if (!el) return;
  const v = binding.fmt ? binding.fmt(value) : value;
  switch (binding.type) {
    case 'text':    el.textContent = v; break;
    case 'style':   el.style[binding.prop] = v; break;
    case 'visible': el.style.display = value ? '' : 'none'; break;
    case 'class':   el.className = (binding.prefix || '') + (value || '') + (binding.suffix || ''); break;
    case 'toast':
      if (binding.condition(value))
        showToast(binding.msg(value), binding.severity);
      break;
  }
}

function render(data) {
  for (const b of BINDINGS) {
    const v = getNested(data, b.key);
    if (v !== undefined) applyBinding(b, v);
  }
  // Inject timestamp (not from API, generated each render)
  const tsEl = document.getElementById('ts');
  if (tsEl && !data.ts_now) {
    tsEl.textContent = new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  }
}

/* ── Toast Notification ──────────────────────── */

let toastTimer = null;

function showToast(message, severity) {
  const container = document.getElementById('toast');
  if (!container) return;
  container.textContent = message;
  container.className = 'toast-banner ' + (severity || 'info') + ' show';
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => container.classList.remove('show'), 2800);
}

/* ── Exports (global scope for app.js) ──────── */
window.BINDINGS   = BINDINGS;
window.render     = render;
window.showToast  = showToast;
window.getNested  = getNested;