"""
edge_llm/dashboard.py — Self-contained web dashboard for EdgeLLM.

v3.1.1: Fixed ring rendering, compact layout, rich history, system metrics.
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
<title>EdgeLLM</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

:root {
  --bg: #0e1015;
  --surface: #161921;
  --card: #1a1e2a;
  --border: rgba(255,255,255,0.05);
  --glow-g: rgba(52,211,153,0.08);
  --glow-b: rgba(96,165,250,0.08);
  --shadow-d: rgba(0,0,0,0.55);
  --shadow-l: rgba(255,255,255,0.02);
  --green: #34d399; --red: #f87171; --yellow: #fbbf24;
  --blue: #60a5fa; --purple: #a78bfa; --cyan: #22d3ee;
  --text: #e8ecf4; --text2: #9ca3af; --muted: #6b7280;
}

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, sans-serif;
  background: var(--bg); color: var(--text);
  padding: 20px 24px; min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

/* ── Header ──────────────────────────── */
.hdr { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.hdr-l { display: flex; align-items: center; gap: 12px; }
.logo {
  width: 34px; height: 34px; border-radius: 9px;
  background: linear-gradient(135deg, #60a5fa, #a78bfa);
  display: flex; align-items: center; justify-content: center;
  font-size: 16px; font-weight: 800; color: #fff;
  box-shadow: 0 4px 16px rgba(96,165,250,0.3);
}
.hdr h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.03em; }
.badge {
  padding: 4px 12px; border-radius: 20px; font-size: 11px; font-weight: 600;
  transition: all 0.3s;
}
.badge.healthy { background: rgba(52,211,153,0.1); color: var(--green); }
.badge.switching { background: rgba(251,191,36,0.1); color: var(--yellow); animation: bpulse 1.5s infinite; }
.badge.idle { background: rgba(107,114,128,0.1); color: var(--muted); }
.badge.error { background: rgba(248,113,113,0.1); color: var(--red); }
@keyframes bpulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
.hdr-r { font-size: 11px; color: var(--muted); }

/* ── 2.5D Card ──────────────────────── */
.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 18px 20px;
  box-shadow: 6px 6px 20px var(--shadow-d), -3px -3px 8px var(--shadow-l);
  transition: transform 0.2s, box-shadow 0.2s;
  position: relative;
}
.card::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.04), transparent);
}
.card:hover {
  transform: translateY(-1px);
  box-shadow: 8px 8px 28px var(--shadow-d), -4px -4px 12px var(--shadow-l);
}
.clbl { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); margin-bottom: 12px; }

/* ── Metrics Row ───────────────────── */
.metrics { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; margin-bottom: 16px; }
.mcard { text-align: center; }
.ring-wrap { position: relative; width: 88px; height: 88px; margin: 0 auto 6px; }
.ring-wrap svg { transform: rotate(-90deg); }
.ring-bg { fill: none; stroke: rgba(255,255,255,0.04); stroke-width: 7; }
.ring-fg { fill: none; stroke-width: 7; stroke-linecap: round; transition: stroke-dashoffset 1s cubic-bezier(.4,0,.2,1), stroke 0.5s; }
.ring-center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
.ring-pct { font-size: 18px; font-weight: 700; letter-spacing: -0.02em; }
.ring-sub { font-size: 9px; color: var(--muted); margin-top: 1px; }
.m-detail { font-size: 12px; color: var(--text2); font-variant-numeric: tabular-nums; }
.m-detail strong { color: var(--text); font-weight: 600; }

/* ── Status Row ────────────────────── */
.status-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 16px; }
.svc { display: flex; align-items: center; gap: 8px; padding: 6px 0; }
.svc + .svc { border-top: 1px solid var(--border); }
.svc-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.svc-dot.on { background: var(--green); box-shadow: 0 0 8px var(--green); }
.svc-dot.off { background: var(--muted); opacity: 0.4; }
.svc-dot.load { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); animation: bpulse 1.5s infinite; }
.svc-name { font-size: 13px; font-weight: 500; flex: 1; }
.svc-pid { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.pf-name { font-size: 22px; font-weight: 700; margin-bottom: 2px; }
.pf-desc { font-size: 12px; color: var(--text2); }

/* ── Switcher ──────────────────────── */
.sw-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
.sw-btn {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 11px; padding: 14px 10px; cursor: pointer;
  color: var(--text); text-align: center;
  box-shadow: 4px 4px 12px var(--shadow-d), -2px -2px 6px var(--shadow-l);
  transition: all 0.2s; position: relative; overflow: hidden;
}
.sw-btn::before {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(135deg, rgba(255,255,255,0.02), transparent);
  pointer-events: none;
}
.sw-btn:hover { transform: translateY(-2px); border-color: rgba(96,165,250,0.25); }
.sw-btn.active {
  border-color: rgba(52,211,153,0.35);
  background: linear-gradient(135deg, rgba(52,211,153,0.06), rgba(96,165,250,0.03));
  box-shadow: 4px 4px 12px var(--shadow-d), 0 0 16px rgba(52,211,153,0.06);
}
.sw-btn.loading { opacity: 0.4; pointer-events: none; }
.sw-nm { font-size: 12px; font-weight: 600; margin-bottom: 4px; }
.sw-ds { font-size: 9.5px; color: var(--muted); line-height: 1.3; }
.sw-ct { font-size: 10px; color: var(--yellow); margin-top: 6px; }
.sw-active-dot {
  position: absolute; top: 7px; right: 7px; width: 5px; height: 5px;
  border-radius: 50%; background: var(--green); box-shadow: 0 0 5px var(--green);
}

/* ── Actions ───────────────────────── */
.acts { display: flex; gap: 10px; margin-bottom: 16px; }
.abtn {
  flex: 1; padding: 10px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 10px;
  cursor: pointer; color: var(--text2); font-size: 12px; font-weight: 500;
  text-align: center; box-shadow: 3px 3px 8px var(--shadow-d);
  transition: all 0.15s;
}
.abtn:hover { transform: translateY(-1px); color: var(--text); }
.abtn.w { border-color: rgba(251,191,36,0.2); }
.abtn.w:hover { color: var(--yellow); }
.abtn.d { border-color: rgba(248,113,113,0.2); }
.abtn.d:hover { color: var(--red); }

/* ── History ───────────────────────── */
.hlist { max-height: 200px; overflow-y: auto; }
.hlist::-webkit-scrollbar { width: 3px; }
.hlist::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.08); border-radius: 2px; }
.hi {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-radius: 8px; margin-bottom: 4px;
  font-size: 11px; background: var(--surface); border: 1px solid var(--border);
}
.hi-fr { color: var(--red); font-weight: 500; min-width: 72px; }
.hi-ar { color: var(--muted); }
.hi-to { color: var(--green); font-weight: 500; min-width: 72px; }
.hi-du { color: var(--cyan); font-variant-numeric: tabular-nums; min-width: 44px; }
.hi-ok { color: var(--green); font-size: 10px; }
.hi-er { color: var(--red); font-size: 10px; }
.hi-tm { margin-left: auto; color: var(--muted); font-size: 10px; }

/* ── Toast ─────────────────────────── */
.toast {
  position: fixed; bottom: 24px; right: 24px;
  padding: 12px 20px; border-radius: 10px;
  font-size: 12px; font-weight: 500;
  transform: translateY(80px); transition: transform 0.3s cubic-bezier(.4,0,.2,1);
  z-index: 100; box-shadow: 0 6px 24px rgba(0,0,0,0.4);
}
.toast.show { transform: translateY(0); }
.toast.success { background: rgba(52,211,153,0.12); color: var(--green); border: 1px solid rgba(52,211,153,0.2); }
.toast.error { background: rgba(248,113,113,0.12); color: var(--red); border: 1px solid rgba(248,113,113,0.2); }

@media (max-width: 900px) {
  .status-row { grid-template-columns: 1fr; }
  .sw-grid { grid-template-columns: repeat(3, 1fr); }
  .metrics { grid-template-columns: 1fr; }
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
  <div class="hdr-r">5s · <span id="ts">-</span></div>
</div>

<!-- Metrics -->
<div class="metrics">
  <div class="card mcard">
    <div class="clbl">GPU 显存</div>
    <div class="ring-wrap">
      <svg width="88" height="88" viewBox="0 0 88 88">
        <circle class="ring-bg" cx="44" cy="44" r="36"/>
        <circle class="ring-fg" id="gR" cx="44" cy="44" r="36" stroke="var(--green)" stroke-dasharray="226.2" stroke-dashoffset="226.2"/>
      </svg>
      <div class="ring-center"><span class="ring-pct" id="gP">0%</span><span class="ring-sub">已用</span></div>
    </div>
    <div class="m-detail"><strong id="gU">0</strong> / <span id="gT">32,607</span> MB</div>
  </div>

  <div class="card mcard">
    <div class="clbl">系统内存</div>
    <div class="ring-wrap">
      <svg width="88" height="88" viewBox="0 0 88 88">
        <circle class="ring-bg" cx="44" cy="44" r="36"/>
        <circle class="ring-fg" id="rR" cx="44" cy="44" r="36" stroke="var(--blue)" stroke-dasharray="226.2" stroke-dashoffset="226.2"/>
      </svg>
      <div class="ring-center"><span class="ring-pct" id="rP">0%</span><span class="ring-sub">已用</span></div>
    </div>
    <div class="m-detail"><strong id="rU">0</strong> / <span id="rT">-</span> GB</div>
  </div>

  <div class="card mcard">
    <div class="clbl">CPU 负载</div>
    <div class="ring-wrap">
      <svg width="88" height="88" viewBox="0 0 88 88">
        <circle class="ring-bg" cx="44" cy="44" r="36"/>
        <circle class="ring-fg" id="cR" cx="44" cy="44" r="36" stroke="var(--purple)" stroke-dasharray="226.2" stroke-dashoffset="226.2"/>
      </svg>
      <div class="ring-center"><span class="ring-pct" id="cP">0%</span><span class="ring-sub">负载</span></div>
    </div>
    <div class="m-detail"><span id="cC">-</span> 核心 · <span id="cU">-</span></div>
  </div>
</div>

<!-- Status -->
<div class="status-row">
  <div class="card">
    <div class="clbl">当前 Profile</div>
    <div class="pf-name" id="pN">idle</div>
    <div class="pf-desc" id="pD">GPU 空闲</div>
  </div>
  <div class="card">
    <div class="clbl">服务状态</div>
    <div class="svc">
      <span class="svc-dot off" id="vD"></span>
      <span class="svc-name">vLLM</span>
      <span class="svc-pid" id="vP">—</span>
    </div>
    <div class="svc">
      <span class="svc-dot off" id="cD"></span>
      <span class="svc-name">ComfyUI</span>
      <span class="svc-pid" id="cP2">—</span>
    </div>
  </div>
</div>

<!-- Switcher -->
<div class="card" style="margin-bottom:16px">
  <div class="clbl">切换 Profile</div>
  <div class="sw-grid" id="swG"></div>
</div>

<!-- Actions -->
<div class="acts">
  <button class="abtn w" onclick="doReconcile()">🔍 Reconcile</button>
  <button class="abtn d" onclick="doReset()">⏹ Reset</button>
</div>

<!-- History -->
<div class="card">
  <div class="clbl">切换历史</div>
  <div class="hlist" id="hL"><div style="padding:12px;text-align:center;color:var(--muted);font-size:11px">加载中…</div></div>
</div>

<div class="toast" id="toast"></div>

<script>
const C = 2 * Math.PI * 36; // ring circumference = 226.2
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

  // State badge
  const bm = { healthy:'healthy', switching:'switching', idle:'idle', error:'error' };
  const bl = { healthy:'运行中', switching:'切换中', idle:'空闲', error:'异常' };
  const b = document.getElementById('sBadge');
  b.textContent = bl[s.state] || s.state;
  b.className = 'badge ' + (bm[s.state] || 'idle');

  // Profile
  document.getElementById('pN').textContent = s.profile;
  document.getElementById('pD').textContent = s.description || '';

  // Services
  document.getElementById('vD').className = dotCls(s.vllm);
  document.getElementById('cD').className = dotCls(s.comfyui);
  document.getElementById('vP').textContent = s.vllm === '✅' ? 'PID ' + (s.vllm_pid || '?') : '—';
  document.getElementById('cP2').textContent = s.comfyui === '✅' ? 'PID ' + (s.comfyui_pid || '?') : '—';

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
    hl.innerHTML = '<div style="padding:12px;text-align:center;color:var(--muted);font-size:11px">暂无历史</div>';
  } else {
    hl.innerHTML = hist.map(h => {
      const t = h.timestamp ? new Date(h.timestamp) : new Date();
      const ts = t.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
      const d = h.duration != null ? h.duration.toFixed(1)+'s' : '-';
      const st = h.status==='ok' ? '<span class="hi-ok">✓</span>' : '<span class="hi-er">✗</span>';
      return `<div class="hi"><span class="hi-fr">${h.from||'-'}</span><span class="hi-ar">→</span><span class="hi-to">${h.to}</span><span class="hi-du">${d}</span>${st}<span class="hi-tm">${ts}</span></div>`;
    }).join('');
  }

  document.getElementById('ts').textContent = new Date().toLocaleTimeString();
}

async function loadProfiles() {
  const ps = await j('/profiles');
  const g = document.getElementById('swG');
  g.innerHTML = '';
  for (const p of ps) {
    const b = document.createElement('div');
    b.className = 'sw-btn' + (p.current ? ' active' : '');
    b.id = 'sw-' + p.name;
    b.innerHTML =
      (p.current ? '<div class="sw-active-dot"></div>' : '') +
      '<div class="sw-nm">' + p.name.replace(/_/g, ' ') + '</div>' +
      '<div class="sw-ds">' + p.description + '</div>' +
      '<div class="sw-ct">⏱ ~' + p.switch_cost_sec + 's</div>';
    b.onclick = () => doSwitch(p.name);
    g.appendChild(b);
  }
}

async function doSwitch(n) {
  if (sw) return;
  const b = document.getElementById('sw-' + n);
  if (b) b.classList.add('loading');
  sw = true;
  try {
    const r = await j('/switch', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({profile:n}) });
    if (r.status === 'switched') toast('✅ ' + n + ' (' + r.elapsed_sec + 's)', 'success');
    else if (r.status === 'already_active') toast('ℹ️ 已在 ' + n, 'success');
    else toast('❌ ' + (r.message || 'failed'), 'error');
    await Promise.all([load(), loadProfiles()]);
  } catch(e) { toast('❌ ' + e.message, 'error'); }
  sw = false;
  document.querySelectorAll('.sw-btn').forEach(b => b.classList.remove('loading'));
}

async function doReset() {
  if (!confirm('强制重置到 idle？')) return;
  const r = await j('/reset', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({profile:'idle'}) });
  toast(r.status==='reset' ? '✅ 已重置' : '❌ '+(r.message||'fail'), r.status==='reset'?'success':'error');
  await Promise.all([load(), loadProfiles()]);
}

async function doReconcile() {
  const r = await j('/reconcile', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({}) });
  const a = r.actions || [];
  toast(a.length===0 ? '✅ 状态一致' : '🔧 '+a.join('; '), 'success');
  await Promise.all([load(), loadProfiles()]);
}

Promise.all([load(), loadProfiles()]);
setInterval(() => { load(); loadProfiles(); }, 5000);
</script>
</body>
</html>"""
