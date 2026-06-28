"""
edge_llm/dashboard.py — Self-contained web dashboard for EdgeLLM.

v4.0: GPU mode display, dynamic services, model-based switcher.
"""

import json
import time
import logging

log = logging.getLogger("edge_llm.dashboard")

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EdgeLLM v4.0</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

:root {
  --bg: #0e1015;
  --surface: #161921;
  --card: #1a1e2a;
  --border: rgba(255,255,255,0.06);
  --shadow-d: rgba(0,0,0,0.55);
  --shadow-l: rgba(255,255,255,0.025);
  --green: #34d399; --red: #f87171; --yellow: #fbbf24;
  --blue: #60a5fa; --purple: #a78bfa; --cyan: #22d3ee;
  --text: #e8ecf4; --text2: #9ca3af; --muted: #6b7280;
}

* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family:'Inter',-apple-system,sans-serif;
  background:var(--bg); color:var(--text);
  padding:24px 28px; min-height:100vh;
  -webkit-font-smoothing:antialiased;
  font-size:15px;
}

/* ── Header ── */
.hdr { display:flex; align-items:center; justify-content:space-between; margin-bottom:24px; }
.hdr-l { display:flex; align-items:center; gap:14px; }
.logo {
  width:40px; height:40px; border-radius:10px;
  background:linear-gradient(135deg,#60a5fa,#a78bfa);
  display:flex; align-items:center; justify-content:center;
  font-size:19px; font-weight:800; color:#fff;
  box-shadow:0 4px 20px rgba(96,165,250,0.35);
}
.hdr h1 { font-size:24px; font-weight:700; letter-spacing:-0.03em; }
.badge {
  padding:5px 16px; border-radius:20px; font-size:13px; font-weight:600;
  transition:all 0.3s;
}
.badge.idle { background:rgba(107,114,128,0.12); color:var(--muted); }
.badge.exclusive { background:rgba(248,113,113,0.12); color:var(--red); }
.badge.shared { background:rgba(52,211,153,0.12); color:var(--green); }
.badge.error { background:rgba(248,113,113,0.12); color:var(--red); }
@keyframes bpulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
.hdr-r { font-size:13px; color:var(--muted); }

/* ── 2.5D Card ── */
.card {
  background:var(--card); border:1px solid var(--border);
  border-radius:16px; padding:22px 24px;
  box-shadow:6px 6px 20px var(--shadow-d),-3px -3px 8px var(--shadow-l);
  transition:transform 0.2s,box-shadow 0.2s;
  position:relative;
}
.card::after {
  content:''; position:absolute; top:0; left:0; right:0; height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.05),transparent);
}
.card:hover {
  transform:translateY(-1px);
  box-shadow:8px 8px 28px var(--shadow-d),-4px -4px 12px var(--shadow-l);
}
.clbl {
  font-size:12px; font-weight:600; text-transform:uppercase;
  letter-spacing:0.08em; color:var(--muted); margin-bottom:14px;
}

/* ── Metrics ── */
.metrics { display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px; margin-bottom:20px; }
.mcard { text-align:center; }
.ring-wrap { position:relative; width:100px; height:100px; margin:0 auto 8px; }
.ring-wrap svg { transform:rotate(-90deg); }
.ring-bg { fill:none; stroke:rgba(255,255,255,0.05); stroke-width:7; }
.ring-fg { fill:none; stroke-width:7; stroke-linecap:round; transition:stroke-dashoffset 1s cubic-bezier(.4,0,.2,1),stroke 0.5s; }
.ring-center { position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center; }
.ring-pct { font-size:22px; font-weight:700; letter-spacing:-0.02em; }
.ring-sub { font-size:11px; color:var(--muted); margin-top:2px; }
.m-detail { font-size:14px; color:var(--text2); font-variant-numeric:tabular-nums; }
.m-detail strong { color:var(--text); font-weight:600; }

/* ── GPU Mode + Services ── */
.profile-row { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:20px; }
.pf-left { display:flex; flex-direction:column; justify-content:center; }
.pf-name { font-size:26px; font-weight:700; margin-bottom:4px; letter-spacing:-0.01em; }
.pf-desc { font-size:14px; color:var(--text2); }
.pf-right { display:flex; flex-direction:column; justify-content:center; gap:12px; }
.svc { display:flex; align-items:center; gap:10px; }
.svc-dot {
  width:9px; height:9px; border-radius:50%; flex-shrink:0;
}
.svc-dot.on { background:var(--green); box-shadow:0 0 10px var(--green); }
.svc-dot.off { background:var(--muted); opacity:0.35; }
.svc-name { font-size:15px; font-weight:500; flex:1; }
.svc-pid { font-size:13px; color:var(--muted); font-variant-numeric:tabular-nums; }
.svc-mode { font-size:11px; padding:2px 8px; border-radius:8px; font-weight:600; }
.svc-mode.excl { background:rgba(248,113,113,0.1); color:var(--red); }
.svc-mode.shrd { background:rgba(52,211,153,0.1); color:var(--green); }

/* ── Switcher ── */
.sw-section { margin-bottom:8px; }
.sw-section-label { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.08em; color:var(--muted); margin-bottom:8px; }
.sw-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:12px; margin-bottom:16px; }
.sw-btn {
  background:var(--surface); border:1px solid var(--border);
  border-radius:12px; padding:16px 12px; cursor:pointer;
  color:var(--text); text-align:center;
  box-shadow:4px 4px 14px var(--shadow-d),-2px -2px 6px var(--shadow-l);
  transition:all 0.2s; position:relative; overflow:hidden;
}
.sw-btn::before {
  content:''; position:absolute; inset:0;
  background:linear-gradient(135deg,rgba(255,255,255,0.025),transparent);
  pointer-events:none;
}
.sw-btn:hover { transform:translateY(-2px); border-color:rgba(96,165,250,0.3); }
.sw-btn.active {
  border-color:rgba(52,211,153,0.4);
  background:linear-gradient(135deg,rgba(52,211,153,0.07),rgba(96,165,250,0.03));
  box-shadow:4px 4px 14px var(--shadow-d),0 0 20px rgba(52,211,153,0.08);
}
.sw-btn.loading { opacity:0.4; pointer-events:none; }
.sw-nm { font-size:14px; font-weight:600; margin-bottom:5px; }
.sw-ds { font-size:11px; color:var(--muted); line-height:1.4; }
.sw-active-dot {
  position:absolute; top:8px; right:8px; width:6px; height:6px;
  border-radius:50%; background:var(--green); box-shadow:0 0 6px var(--green);
}

/* ── Actions ── */
.acts { display:flex; gap:12px; margin-bottom:20px; }
.abtn {
  flex:1; padding:12px; background:var(--surface);
  border:1px solid var(--border); border-radius:11px;
  cursor:pointer; color:var(--text2); font-size:14px; font-weight:500;
  text-align:center; box-shadow:3px 3px 10px var(--shadow-d);
  transition:all 0.15s;
}
.abtn:hover { transform:translateY(-1px); color:var(--text); }
.abtn.w { border-color:rgba(251,191,36,0.25); }
.abtn.w:hover { color:var(--yellow); }
.abtn.d { border-color:rgba(248,113,113,0.25); }
.abtn.d:hover { color:var(--red); }

/* ── History ── */
.hlist { max-height:220px; overflow-y:auto; }
.hlist::-webkit-scrollbar { width:4px; }
.hlist::-webkit-scrollbar-thumb { background:rgba(255,255,255,0.08); border-radius:2px; }
.hi {
  display:flex; align-items:center; gap:10px;
  padding:10px 14px; border-radius:9px; margin-bottom:5px;
  font-size:13px; background:var(--surface); border:1px solid var(--border);
}
.hi-fr { color:var(--red); font-weight:500; min-width:80px; }
.hi-ar { color:var(--muted); font-size:15px; }
.hi-to { color:var(--green); font-weight:500; min-width:80px; }
.hi-du { color:var(--cyan); font-variant-numeric:tabular-nums; min-width:50px; }
.hi-ok { color:var(--green); font-size:12px; }
.hi-er { color:var(--red); font-size:12px; }
.hi-tm { margin-left:auto; color:var(--muted); font-size:12px; }

/* ── Toast ── */
.toast {
  position:fixed; bottom:28px; right:28px;
  padding:14px 24px; border-radius:12px;
  font-size:14px; font-weight:500;
  transform:translateY(80px); transition:transform 0.3s cubic-bezier(.4,0,.2,1);
  z-index:100; box-shadow:0 8px 28px rgba(0,0,0,0.45);
}
.toast.show { transform:translateY(0); }
.toast.success { background:rgba(52,211,153,0.12); color:var(--green); border:1px solid rgba(52,211,153,0.25); }
.toast.error { background:rgba(248,113,113,0.12); color:var(--red); border:1px solid rgba(248,113,113,0.25); }

@media (max-width:900px) {
  .profile-row { grid-template-columns:1fr; }
  .sw-grid { grid-template-columns:repeat(3,1fr); }
  .metrics { grid-template-columns:1fr; }
}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-l">
    <div class="logo">E</div>
    <h1>EdgeLLM</h1>
    <span class="badge idle" id="sBadge">空闲</span>
  </div>
  <div class="hdr-r">v4.0 · 5s · <span id="ts">-</span></div>
</div>

<!-- Metrics -->
<div class="metrics">
  <div class="card mcard">
    <div class="clbl">GPU 显存</div>
    <div class="ring-wrap">
      <svg width="100" height="100" viewBox="0 0 100 100">
        <circle class="ring-bg" cx="50" cy="50" r="40"/>
        <circle class="ring-fg" id="gR" cx="50" cy="50" r="40" stroke="var(--green)" stroke-dasharray="251.3" stroke-dashoffset="251.3"/>
      </svg>
      <div class="ring-center"><span class="ring-pct" id="gP">0%</span><span class="ring-sub">已用</span></div>
    </div>
    <div class="m-detail"><strong id="gU">0</strong> / <span id="gT">32,607</span> MB</div>
  </div>

  <div class="card mcard">
    <div class="clbl">系统内存</div>
    <div class="ring-wrap">
      <svg width="100" height="100" viewBox="0 0 100 100">
        <circle class="ring-bg" cx="50" cy="50" r="40"/>
        <circle class="ring-fg" id="rR" cx="50" cy="50" r="40" stroke="var(--blue)" stroke-dasharray="251.3" stroke-dashoffset="251.3"/>
      </svg>
      <div class="ring-center"><span class="ring-pct" id="rP">0%</span><span class="ring-sub">已用</span></div>
    </div>
    <div class="m-detail"><strong id="rU">0</strong> / <span id="rT">-</span> GB</div>
  </div>

  <div class="card mcard">
    <div class="clbl">CPU 负载</div>
    <div class="ring-wrap">
      <svg width="100" height="100" viewBox="0 0 100 100">
        <circle class="ring-bg" cx="50" cy="50" r="40"/>
        <circle class="ring-fg" id="cR" cx="50" cy="50" r="40" stroke="var(--purple)" stroke-dasharray="251.3" stroke-dashoffset="251.3"/>
      </svg>
      <div class="ring-center"><span class="ring-pct" id="cP">0%</span><span class="ring-sub">负载</span></div>
    </div>
    <div class="m-detail"><span id="cC">-</span> 核心 · <span id="cU">-</span></div>
  </div>
</div>

<!-- GPU Mode + Services -->
<div class="profile-row">
  <div class="card">
    <div class="pf-left">
      <div class="clbl">GPU 模式</div>
      <div class="pf-name" id="pN">idle</div>
      <div class="pf-desc" id="pD">GPU 空闲</div>
    </div>
  </div>
  <div class="card">
    <div class="pf-right">
      <div class="clbl">活跃服务</div>
      <div id="svcList">
        <div style="padding:8px;text-align:center;color:var(--muted);font-size:13px">无</div>
      </div>
    </div>
  </div>
</div>

<!-- Switcher -->
<div class="card" style="margin-bottom:20px">
  <div class="clbl">切换模型</div>
  <div id="swArea"></div>
</div>

<!-- Actions -->
<div class="acts">
  <button class="abtn w" onclick="doReconcile()">🔍 Reconcile</button>
  <button class="abtn d" onclick="doReset()">⏹ Reset</button>
</div>

<!-- History -->
<div class="card">
  <div class="clbl">切换历史</div>
  <div class="hlist" id="hL"><div style="padding:14px;text-align:center;color:var(--muted);font-size:13px">加载中…</div></div>
</div>

<div class="toast" id="toast"></div>

<script>
const C = 2 * Math.PI * 40;
let sw = false;

function toast(m, t) {
  const e = document.getElementById('toast');
  e.textContent = m; e.className = 'toast ' + t + ' show';
  setTimeout(() => e.classList.remove('show'), 3000);
}

async function j(p, o) { return (await fetch(p, o)).json(); }

function ring(id, pct) {
  const el = document.getElementById(id);
  const v = Math.min(Math.max(pct, 0), 100);
  el.style.strokeDashoffset = C * (1 - v / 100);
  if (id === 'gR') el.style.stroke = v < 50 ? 'var(--green)' : v < 80 ? 'var(--yellow)' : 'var(--red)';
}

function dotCls(s) { return s === '✅' ? 'svc-dot on' : s === '⏳' ? 'svc-dot load' : 'svc-dot off'; }

async function load() {
  const [s, sys, hist] = await Promise.all([
    j('/status'), j('/system').catch(() => ({})), j('/history').catch(() => [])
  ]);

  // GPU mode badge
  const modeMap = {
    idle: { cls:'idle', label:'⚪ idle' },
    exclusive: { cls:'exclusive', label:'🔒 exclusive' },
    shared: { cls:'shared', label:'🔓 shared' }
  };
  const gm = s.gpu_mode || 'idle';
  const modeInfo = modeMap[gm] || { cls:'idle', label:gm };
  const b = document.getElementById('sBadge');
  b.textContent = modeInfo.label;
  b.className = 'badge ' + modeInfo.cls;

  // GPU mode display
  document.getElementById('pN').textContent = gm;
  const descMap = { idle:'GPU 空闲', exclusive:'独占模式 — 单模型锁定 GPU', shared:'共享模式 — 多服务共存' };
  document.getElementById('pD').textContent = descMap[gm] || '';

  // Services (dynamic)
  const svcList = document.getElementById('svcList');
  const services = s.active_services || [];
  const health = s.services_health || {};
  if (services.length === 0) {
    svcList.innerHTML = '<div style="padding:8px;text-align:center;color:var(--muted);font-size:13px">无活跃服务</div>';
  } else {
    svcList.innerHTML = services.map(name => {
      const h = health[name] || '❌';
      const dot = dotCls(h);
      return '<div class="svc"><span class="' + dot + '"></span><span class="svc-name">' + name + '</span><span class="svc-pid">' + (h === '✅' ? '运行中' : h) + '</span></div>';
    }).join('');
  }

  // GPU
  const gt = s.gpu_total_mb || 32607, gu = s.gpu_used_mb || 0;
  const gp = gu / gt * 100;
  document.getElementById('gU').textContent = gu.toLocaleString();
  document.getElementById('gT').textContent = gt.toLocaleString();
  document.getElementById('gP').textContent = gp.toFixed(1) + '%';
  ring('gR', gp);

  // RAM
  const rt = sys.ram_total_gb || 1, ru = sys.ram_used_gb || 0;
  const rp = ru / rt * 100;
  document.getElementById('rU').textContent = ru.toFixed(1);
  document.getElementById('rT').textContent = rt.toFixed(1);
  document.getElementById('rP').textContent = rp.toFixed(1) + '%';
  ring('rR', rp);

  // CPU
  const cp = sys.cpu_percent || 0;
  document.getElementById('cP').textContent = cp.toFixed(1) + '%';
  document.getElementById('cC').textContent = sys.cpu_cores || '-';
  ring('cR', cp);
  const us = sys.uptime_seconds || 0;
  document.getElementById('cU').textContent = Math.floor(us/3600) + 'h ' + Math.floor((us%3600)/60) + 'm';

  // History
  const hl = document.getElementById('hL');
  if (!hist || hist.length === 0) {
    hl.innerHTML = '<div style="padding:14px;text-align:center;color:var(--muted);font-size:13px">暂无历史</div>';
  } else {
    hl.innerHTML = hist.map(h => {
      const t = h.timestamp ? new Date(h.timestamp) : new Date();
      const ts = t.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
      const d = h.duration != null ? h.duration.toFixed(1)+'s' : '-';
      const st = h.status==='ok' ? '<span class="hi-ok">✓</span>' : '<span class="hi-er">✗</span>';
      return '<div class="hi"><span class="hi-fr">' + (h.from||'-') + '</span><span class="hi-ar">→</span><span class="hi-to">' + h.to + '</span><span class="hi-du">' + d + '</span>' + st + '<span class="hi-tm">' + ts + '</span></div>';
    }).join('');
  }

  document.getElementById('ts').textContent = new Date().toLocaleTimeString();
}

async function loadModels() {
  const models = await j('/models');
  const area = document.getElementById('swArea');

  // Group by mode
  const exclusive = models.filter(m => m.mode === 'exclusive');
  const shared = models.filter(m => m.mode === 'shared');

  let html = '';

  // Idle button
  html += '<div class="sw-grid">';
  html += '<div class="sw-btn" id="sw-idle" onclick="doSwitch(\'idle\')">';
  html += '<div class="sw-nm">⚪ idle</div>';
  html += '<div class="sw-ds">释放 GPU</div>';
  html += '</div>';
  html += '</div>';

  // Exclusive models
  if (exclusive.length > 0) {
    html += '<div class="sw-section-label">🔒 独占模型</div>';
    html += '<div class="sw-grid">';
    for (const m of exclusive) {
      const isActive = m.active;
      html += '<div class="sw-btn' + (isActive ? ' active' : '') + '" id="sw-' + m.name + '" onclick="doSwitch(\'' + m.name + '\')">';
      if (isActive) html += '<div class="sw-active-dot"></div>';
      html += '<div class="sw-nm">' + m.name + '</div>';
      html += '<div class="sw-ds">' + (m.description || '') + '</div>';
      html += '</div>';
    }
    html += '</div>';
  }

  // Shared models
  if (shared.length > 0) {
    html += '<div class="sw-section-label">🔓 共享服务</div>';
    html += '<div class="sw-grid">';
    for (const m of shared) {
      const isActive = m.active;
      html += '<div class="sw-btn' + (isActive ? ' active' : '') + '" id="sw-' + m.name + '" onclick="doSwitch(\'' + m.name + '\')">';
      if (isActive) html += '<div class="sw-active-dot"></div>';
      html += '<div class="sw-nm">' + m.name + '</div>';
      html += '<div class="sw-ds">' + (m.description || '') + '</div>';
      html += '</div>';
    }
    html += '</div>';
  }

  area.innerHTML = html;
}

async function doSwitch(n) {
  if (sw) return;
  const b = document.getElementById('sw-' + n);
  if (b) b.classList.add('loading');
  sw = true;
  try {
    const r = await j('/switch', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({model:n}) });
    if (r.status === 'switched') toast('✅ ' + n + ' (' + r.elapsed_sec + 's)', 'success');
    else if (r.status === 'already_active') toast('ℹ️ 已在 ' + n, 'success');
    else toast('❌ ' + (r.message || 'failed'), 'error');
    await Promise.all([load(), loadModels()]);
  } catch(e) { toast('❌ ' + e.message, 'error'); }
  sw = false;
  document.querySelectorAll('.sw-btn').forEach(b => b.classList.remove('loading'));
}

async function doReset() {
  if (!confirm('强制重置到 idle？')) return;
  const r = await j('/reset', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({}) });
  toast(r.status==='reset' ? '✅ 已重置' : '❌ '+(r.message||'fail'), r.status==='reset'?'success':'error');
  await Promise.all([load(), loadModels()]);
}

async function doReconcile() {
  const r = await j('/reconcile', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({}) });
  const a = r.actions || [];
  toast(a.length===0 ? '✅ 状态一致' : '🔧 '+a.join('; '), 'success');
  await Promise.all([load(), loadModels()]);
}

Promise.all([load(), loadModels()]);
setInterval(() => { load(); loadModels(); }, 5000);
</script>
</body>
</html>"""
