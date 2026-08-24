// ── Monitor Panel — G-3b: 7-panel metrics dashboard ──

let _metricsWindow = '24h';
const VALID_WINDOWS = new Set(['1h', '24h', '7d', 'all']);

// Window toggle
document.addEventListener('click', function(e) {
  if (e.target.classList.contains('m-wtab')) {
    const w = e.target.dataset.w;
    if (!VALID_WINDOWS.has(w)) return;
    document.querySelectorAll('.m-wtab').forEach(b => b.classList.remove('active'));
    e.target.classList.add('active');
    _metricsWindow = w;
    loadMetrics();
  }
});

// Visibility-based polling — only fetch when tab is visible
let _metricsInterval = null;
function startMetricsPolling() {
  if (_metricsInterval) return;
  loadMetrics();
  _metricsInterval = setInterval(loadMetrics, 5000);
}
function stopMetricsPolling() {
  if (_metricsInterval) { clearInterval(_metricsInterval); _metricsInterval = null; }
}
document.addEventListener('visibilitychange', function() {
  if (document.hidden) stopMetricsPolling(); else startMetricsPolling();
});

// ── XSS-safe escape ──
function esc(v) {
  if (v == null) return '';
  const d = document.createElement('div');
  d.textContent = String(v);
  return d.innerHTML;
}

// ── Fetch & render ──
async function loadMetrics() {
  try {
    const r = await fetch('/api/metrics?window=' + encodeURIComponent(_metricsWindow));
    if (!r.ok) return;
    const d = await r.json();
    renderOverview(d);
    renderTokens(d);
    renderLatency(d);
    renderCost(d);
  } catch(e) { /* ignore */ }
}

function renderOverview(d) {
  const el = id => document.getElementById(id);
  el('ovTotal').textContent = d.total_requests || 0;
  el('ovSuccess').textContent = d.success || 0;
  el('ovFail').textContent = d.fail || 0;
  el('ovRate').textContent = (d.success_rate || 0) + '%';
  el('ovRate').style.color = (d.success_rate || 0) >= 95 ? 'var(--green)' :
                              (d.success_rate || 0) >= 80 ? 'var(--orange)' : 'var(--red)';
}

function renderTokens(d) {
  const models = d.models || {};
  if (!Object.keys(models).length) {
    document.getElementById('tokenTable').innerHTML = '<div class="m-empty">暂无数据</div>';
    return;
  }
  let html = '<table class="m-tbl"><tr><th>模型</th><th>请求</th><th>输入 Tokens</th><th>输出 Tokens</th></tr>';
  for (const [m, v] of Object.entries(models)) {
    const name = m.split('/').pop();
    html += '<tr><td>' + esc(name) + '</td><td>' + esc(v.requests) + '</td><td>' + esc(v.tokens_in) + '</td><td>' + esc(v.tokens_out) + '</td></tr>';
  }
  html += '</table>';
  document.getElementById('tokenTable').innerHTML = html;
}

function renderLatency(d) {
  const models = d.models || {};
  if (!Object.keys(models).length) {
    document.getElementById('latencyTable').innerHTML = '<div class="m-empty">暂无数据</div>';
    return;
  }
  let html = '<table class="m-tbl"><tr><th>模型</th><th>TTFT p50</th><th>TTFT p95</th><th>TTFT p99</th><th>E2E p50</th><th>E2E p95</th></tr>';
  for (const [m, v] of Object.entries(models)) {
    const name = m.split('/').pop();
    html += '<tr><td>' + esc(name) + '</td>' +
      '<td>' + esc(v.ttft_p50 != null ? v.ttft_p50 + 'ms' : '—') + '</td>' +
      '<td>' + esc(v.ttft_p95 != null ? v.ttft_p95 + 'ms' : '—') + '</td>' +
      '<td>' + esc(v.ttft_p99 != null ? v.ttft_p99 + 'ms' : '—') + '</td>' +
      '<td>' + esc(v.duration_p50 != null ? v.duration_p50 + 'ms' : '—') + '</td>' +
      '<td>' + esc(v.duration_p95 != null ? v.duration_p95 + 'ms' : '—') + '</td></tr>';
  }
  html += '</table>';
  document.getElementById('latencyTable').innerHTML = html;
}

function renderCost(d) {
  document.getElementById('costTotal').textContent = '¥' + (d.cost_yuan || 0).toFixed(4);
  const models = d.models || {};
  let html = '';
  for (const [m, v] of Object.entries(models)) {
    if (v.cost_yuan > 0) {
      const name = m.split('/').pop();
      html += '<div class="m-cost-row"><span>' + esc(name) + '</span><span>¥' + esc(v.cost_yuan.toFixed(4)) + '</span></div>';
    }
  }
  document.getElementById('costBreakdown').innerHTML = html || '<div class="m-empty">暂无费用数据</div>';
}

// ── Request Log Panel ──
let _logInterval = null;
async function loadRequestLog() {
  try {
    // v5.x: prefer the /api/snapshot store (same 50-entry/1h window as the
    // old /api/request_log poll); fall back to a direct fetch when the
    // snapshot has no request_log yet.
    let logs = window.store && window.store.get('request_log');
    if (!logs || !logs.length) {
      const since = Math.floor(Date.now() / 1000) - 3600; // last 1h
      const r = await fetch('/api/request_log?limit=50&since=' + since);
      if (!r.ok) return;
      const d = await r.json();
      logs = d.logs || [];
    }
    const el = document.getElementById('logBody');
    if (!logs.length) {
      el.innerHTML = '<div class="m-empty">暂无请求日志</div>';
    } else {
      let html = '<table class="m-tbl"><tr><th>时间</th><th>模型</th><th>状态</th><th>Tokens</th><th>TTFT</th><th>耗时</th></tr>';
      for (const l of logs) {
        const ts = l.timestamp ? new Date(l.timestamp * 1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
        const model = (l.model || '').split('/').pop();
        const statusCls = l.status < 400 ? 'color:var(--green)' : 'color:var(--red)';
        const tokens = (l.tokens_in || 0) + '/' + (l.tokens_out || 0);
        const ttft = l.ttft_ms != null ? l.ttft_ms.toFixed(0) + 'ms' : '—';
        const dur = l.duration_ms != null ? l.duration_ms.toFixed(0) + 'ms' : '—';
        html += '<tr><td>' + esc(ts) + '</td><td>' + esc(model) + '</td><td style="' + statusCls + '">' + esc(l.status) + '</td><td>' + esc(tokens) + '</td><td>' + esc(ttft) + '</td><td>' + esc(dur) + '</td></tr>';
      }
      html += '</table>';
      el.innerHTML = html;
    }
    document.getElementById('logTs').textContent = new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  } catch(e) { /* ignore */ }
}
function startLogPolling() {
  if (_logInterval) return;
  loadRequestLog();
  _logInterval = setInterval(loadRequestLog, 5000);
}
function stopLogPolling() {
  if (_logInterval) { clearInterval(_logInterval); _logInterval = null; }
}
document.addEventListener('visibilitychange', function() {
  if (document.hidden) stopLogPolling(); else startLogPolling();
});

// ── Init ──
startMetricsPolling();
startLogPolling();
