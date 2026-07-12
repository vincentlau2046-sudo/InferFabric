"""
inferfabric/dashboard.py — Self-contained web dashboard for InferFabric.

v5.0: Light Terminal UI theme. Warm terracotta palette, monospace typography,
      terminal-window cards, macOS dots, syntax-highlight tags.
"""

import json
import time
import logging

log = logging.getLogger("inferfabric.dashboard")

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>InferFabric</title>
<style>
:root {
  --primary:   #cc7a60;
  --primary-l: #d99178;
  --primary-s: rgba(204,122,96,.12);
  --primary-g: linear-gradient(135deg, #e5a890, #cc7a60);
  --green:     #16a34a;
  --green-s:   rgba(22,163,74,.10);
  --green-g:   linear-gradient(135deg, #4ade80, #16a34a);
  --red:       #dc2626;
  --red-s:     rgba(220,38,38,.10);
  --red-g:     linear-gradient(135deg, #f87171, #dc2626);
  --orange:    #f59e0b;
  --orange-s:  rgba(245,158,11,.10);
  --orange-g:  linear-gradient(135deg, #fbbf24, #f59e0b);
  --purple:    #9333ea;
  --purple-s:  rgba(147,51,234,.10);
  --purple-g:  linear-gradient(135deg, #c084fc, #9333ea);
  --blue:      #2563eb;
  --blue-s:    rgba(37,99,235,.10);
  --blue-g:    linear-gradient(135deg, #60a5fa, #2563eb);
  --teal:      #0d9488;
  --text1:     #1f2937;
  --text2:     #6b7280;
  --text3:     #9ca3af;
  --text4:     #d1d5db;
  --bg:        #ffffff;
  --bg-card:   #fafaf9;
  --bg-code:   #f5f5f4;
  --border:    #e5e7eb;
  --border-l:  #d1d5db;
  --shadow:    0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
  --shadow-md: 0 4px 12px rgba(0,0,0,.08);
  --radius:    10px;
  --radius-lg: 12px;
  --radius-xl: 16px;
  --font-mono: 'JetBrains Mono', 'Fira Code', 'SF Mono', 'Cascadia Code', 'Consolas', 'Liberation Mono', monospace;
  --dot-red:   #ff5f57;
  --dot-yellow:#febc2e;
  --dot-green: #28c840;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family:var(--font-mono); background:var(--bg); color:var(--text1);
  font-size:14px; line-height:1.5;
}
.terminal-window {
  background:var(--bg-card); border:1px solid var(--border);
  border-radius:var(--radius-lg); box-shadow:var(--shadow);
  overflow:hidden; transition:all .2s ease;
}
.terminal-window:hover {
  border-color:var(--primary); box-shadow:var(--shadow-md); transform:translateY(-1px);
}
.window-header {
  display:flex; align-items:center; gap:8px;
  background:var(--bg-code); padding:8px 12px; border-bottom:1px solid var(--border-l);
}
.window-dots { display:flex; gap:6px; flex-shrink:0; }
.window-dots .dot { width:12px; height:12px; border-radius:50%; display:block; }
.window-dots .dot.red { background:var(--dot-red); }
.window-dots .dot.yellow { background:var(--dot-yellow); }
.window-dots .dot.green { background:var(--dot-green); }
.window-title { flex:1; font-size:13px; color:var(--text2); font-family:var(--font-mono); letter-spacing:.02em; }
.window-status { font-size:11px; color:var(--text3); }
.window-content { padding:14px 16px; }
.nav {
  position:sticky; top:0; z-index:50;
  background:rgba(255,255,255,0.9); backdrop-filter:blur(8px); -webkit-backdrop-filter:blur(8px);
  border-bottom:1px solid var(--border);
}
.nav-in { max-width:1000px; margin:0 auto; display:flex; align-items:center; justify-content:space-between; height:52px; padding:0 28px; }
.nav-l { display:flex; align-items:center; gap:12px; }
.nav-logo { display:flex; align-items:center; gap:8px; }
.nav-logo-dots { display:flex; gap:4px; }
.nav-logo-dots span { width:8px; height:8px; border-radius:50%; display:block; }
.nav-logo-dots span:nth-child(1) { background:var(--dot-red); }
.nav-logo-dots span:nth-child(2) { background:var(--dot-yellow); }
.nav-logo-dots span:nth-child(3) { background:var(--dot-green); }
.nav-title { font-size:18px; font-weight:700; letter-spacing:-.02em; color:var(--text1); }
.nav-r { display:flex; align-items:center; gap:14px; }
.nav-ver { font-size:13px; color:var(--primary); font-weight:600; font-family:var(--font-mono); background:var(--primary-s); padding:2px 8px; border-radius:6px; }
.nav-ts { font-size:13px; color:var(--text3); font-variant-numeric:tabular-nums; font-family:var(--font-mono); }
.tag { display:inline-flex; align-items:center; gap:5px; padding:4px 14px; border-radius:20px; font-size:13px; font-weight:600; font-family:var(--font-mono); transition:all .3s; }
.tag .dot { width:7px; height:7px; border-radius:50%; transition:background .3s; }
.tag.idle { background:var(--bg-code); color:var(--text3); border:1px solid var(--border-l); }
.tag.idle .dot { background:var(--text3); }
.tag.exclusive { background:var(--red-s); color:var(--red); border:1px solid rgba(220,38,38,0.3); }
.tag.exclusive .dot { background:var(--red); }
.tag.shared { background:var(--green-s); color:var(--green); border:1px solid rgba(22,163,74,0.3); }
.tag.shared .dot { background:var(--green); }
.main { max-width:1000px; margin:0 auto; padding:24px 28px 48px; }
.status-bar { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-bottom:20px; }
.stat-card {
  background:var(--bg-card); border:1px solid var(--border);
  border-radius:var(--radius-lg); box-shadow:var(--shadow);
  overflow:hidden; transition:all .2s ease;
}
.stat-card:hover { border-color:var(--primary); box-shadow:var(--shadow-md); transform:translateY(-1px); }
.stat-card-header {
  display:flex; align-items:center; gap:8px;
  background:var(--bg-code); padding:8px 12px; border-bottom:1px solid var(--border-l);
}
.stat-card-header .window-dots { display:flex; gap:6px; flex-shrink:0; }
.stat-card-header .window-dots .dot { width:10px; height:10px; border-radius:50%; display:block; }
.stat-card-header .window-dots .dot.red { background:var(--dot-red); }
.stat-card-header .window-dots .dot.yellow { background:var(--dot-yellow); }
.stat-card-header .window-dots .dot.green { background:var(--dot-green); }
.stat-card-header .stat-label { flex:1; font-size:12px; font-weight:600; color:var(--text2); text-transform:uppercase; letter-spacing:.04em; }
.stat-card-body { padding:14px 14px 12px; }
.stat-row { display:flex; align-items:baseline; gap:6px; margin-bottom:8px; }
.stat-val { font-size:26px; font-weight:700; letter-spacing:-.03em; color:var(--text1); line-height:1; font-variant-numeric:tabular-nums; }
.stat-unit { font-size:14px; font-weight:500; color:var(--text3); }
.stat-bar { height:5px; border-radius:3px; background:var(--bg-code); overflow:hidden; }
.stat-bar-f { height:100%; border-radius:3px; transition:width .8s cubic-bezier(.4,0,.2,1), background .4s; }
.stat-sub { font-size:12px; color:var(--text3); margin-top:5px; font-variant-numeric:tabular-nums; font-family:var(--font-mono); }
.stat-sub b { color:var(--text2); font-weight:600; }
.panel {
  background:var(--bg-card); border:1px solid var(--border);
  border-radius:var(--radius-lg); box-shadow:var(--shadow);
  overflow:hidden; transition:all .2s ease; margin-bottom:14px;
}
.panel:hover { box-shadow:var(--shadow-md); }
.panel-hdr {
  display:flex; align-items:center; gap:8px;
  background:var(--bg-code); padding:8px 12px; border-bottom:1px solid var(--border-l);
}
.panel-hdr::before {
  content:''; display:inline-block; flex-shrink:0; width:28px; height:12px;
  background:
    radial-gradient(circle,var(--dot-red) 50%,transparent 50%) 0 0/10px 10px no-repeat,
    radial-gradient(circle,var(--dot-yellow) 50%,transparent 50%) 14px 0/10px 10px no-repeat,
    radial-gradient(circle,var(--dot-green) 50%,transparent 50%) 28px 0/10px 10px no-repeat;
  background-size:28px 12px;
}
.panel-icon { width:24px; height:24px; border-radius:6px; display:flex; align-items:center; justify-content:center; font-size:12px; color:#fff; flex-shrink:0; }
.panel-icon.excl { background:var(--red-g); box-shadow:0 2px 6px rgba(220,38,38,.2); }
.panel-icon.shrd { background:var(--green-g); box-shadow:0 2px 6px rgba(22,163,74,.2); }
.panel-icon.free { background:var(--orange-g); box-shadow:0 2px 8px rgba(245,158,11,.2); }
.panel-icon.vllm { background:var(--red-g); box-shadow:0 2px 6px rgba(220,38,38,.2); }
.panel-icon.ollama { background:var(--green-g); box-shadow:0 2px 6px rgba(22,163,74,.2); }
.panel-title { font-size:14px; font-weight:600; color:var(--text1); letter-spacing:-.01em; font-family:var(--font-mono); }
.panel-content { padding:14px 16px; }
.model-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; min-height:60px; }
@media (max-width:900px) { .model-grid { grid-template-columns:repeat(2,1fr); } }
@media (max-width:600px) { .model-grid { grid-template-columns:1fr; } }
.model-card {
  background:var(--bg-code); border:1.5px solid var(--border);
  border-radius:var(--radius-lg); padding:12px 14px;
  cursor:pointer; transition:all .2s ease;
}
.model-card:hover { border-color:var(--primary); box-shadow:var(--shadow-md); transform:translateY(-1px); }
.model-card.active { border-color:var(--green); border-left:3px solid var(--green); background:var(--green-s); }
.model-card.active:hover { border-color:var(--green); box-shadow:0 0 0 1px rgba(22,163,74,0.2),var(--shadow-md); }
.model-top { display:flex; align-items:center; gap:8px; }
.model-dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; background:var(--text3); transition:all .3s; }
.model-card.active .model-dot { background:var(--green); box-shadow:0 0 0 3px var(--green-s); }
.model-info { flex:1; min-width:0; }
.model-name { font-size:14px; font-weight:600; color:var(--text1); letter-spacing:-.01em; font-family:var(--font-mono); }
.model-specs { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
.spec-tag {
  display:inline-flex; align-items:center; gap:4px; padding:2px 8px; border-radius:4px;
  background:var(--bg); border:1px solid var(--border);
  font-size:11px; color:var(--text2); font-family:var(--font-mono); white-space:nowrap; font-variant-numeric:tabular-nums;
}
.model-badge { padding:3px 10px; border-radius:6px; font-size:11px; font-weight:600; font-family:var(--font-mono); }
.model-badge.excl { background:var(--red-s); color:var(--red); border:1px solid rgba(220,38,38,0.3); }
.model-badge.shrd { background:var(--green-s); color:var(--green); border:1px solid rgba(22,163,74,0.3); }
.model-badge.free { background:var(--orange-s); color:var(--orange); border:1px solid rgba(245,158,11,0.3); }
.model-actions { display:flex; gap:6px; margin-top:10px; }
.btn-card {
  flex:1; padding:5px 0; border:1px solid var(--border); border-radius:6px;
  font-size:12px; font-weight:500; cursor:pointer; transition:all .15s; font-family:var(--font-mono);
}
.btn-card.start { background:var(--green-s); color:var(--green); }
.btn-card.start:hover { background:rgba(22,163,74,.20); border-color:var(--green); }
.btn-card.start:disabled { opacity:.3; cursor:default; }
.btn-card.stop { background:var(--red-s); color:var(--red); }
.btn-card.stop:hover { background:rgba(220,38,38,.20); border-color:var(--red); }
.btn-card.stop:disabled { opacity:.3; cursor:default; }
.idle-card {
  background:transparent; border:1.5px dashed var(--border);
  border-radius:var(--radius-lg); padding:14px 16px; cursor:pointer; transition:all .2s;
  display:flex; align-items:center; gap:10px; font-family:var(--font-mono);
}
.idle-card:hover { background:var(--bg-code); border-color:var(--border-l); }
.idle-card-icon { font-size:18px; }
.idle-card-text { font-size:13px; font-weight:500; color:var(--text3); }
.fill { font-size:13px; color:var(--text3); text-align:center; padding:14px; font-family:var(--font-mono); }
.svc-empty { font-size:13px; color:var(--text3); padding:8px 0; font-family:var(--font-mono); }
.panels { display:flex; flex-direction:column; gap:14px; margin-bottom:18px; }
.panels .panel { margin-bottom:0; }
.act-row { display:flex; gap:8px; margin-bottom:18px; }
.act-btn {
  padding:8px 20px; border:none; border-radius:var(--radius-lg);
  font-size:13px; font-weight:600; cursor:pointer; transition:all .15s;
  letter-spacing:-.01em; font-family:var(--font-mono);
}
.act-btn.pri { background:var(--primary); color:#fff; box-shadow:0 2px 8px var(--primary-s); }
.act-btn.pri:hover { background:var(--primary-l); }
.act-btn.sec { background:var(--bg-card); color:var(--text1); border:1px solid var(--border); }
.act-btn.sec:hover { background:var(--bg-code); border-color:var(--primary); }
.act-btn.warn { background:var(--red-s); color:var(--red); border:1px solid rgba(220,38,38,0.3); }
.act-btn.warn:hover { background:rgba(220,38,38,.20); }
.perf-panel {
  background:var(--bg-card); border:1px solid var(--border);
  border-radius:var(--radius-lg); box-shadow:var(--shadow);
  overflow:hidden; margin-bottom:18px; transition:all .2s;
}
.perf-panel:hover { box-shadow:var(--shadow-md); }
.perf-panel.sleeping { opacity:.5; }
.perf-hdr {
  display:flex; align-items:center; gap:8px;
  background:var(--bg-code); padding:8px 12px; border-bottom:1px solid var(--border-l);
}
.perf-hdr::before {
  content:''; display:inline-block; flex-shrink:0; width:28px; height:12px;
  background:
    radial-gradient(circle,var(--dot-red) 50%,transparent 50%) 0 0/10px 10px no-repeat,
    radial-gradient(circle,var(--dot-yellow) 50%,transparent 50%) 14px 0/10px 10px no-repeat,
    radial-gradient(circle,var(--dot-green) 50%,transparent 50%) 28px 0/10px 10px no-repeat;
  background-size:28px 12px;
}
.perf-refresh { font-size:11px; color:var(--text3); margin-left:auto; font-family:var(--font-mono); }
.perf-sleep-badge {
  padding:3px 10px; border-radius:6px; font-size:11px; font-weight:600;
  color:var(--purple); background:var(--purple-s); display:none;
  border:1px solid rgba(147,51,234,0.3); font-family:var(--font-mono);
}
.perf-cards { display:grid; grid-template-columns:repeat(5,1fr); gap:10px; padding:14px; }
.perf-card {
  background:var(--bg-code); border:1px solid var(--border);
  border-radius:var(--radius-lg); padding:12px 14px;
  display:flex; flex-direction:column; gap:6px; transition:all .3s; position:relative;
}
.perf-card.warn { background:var(--orange-s); border-color:rgba(245,158,11,0.3); }
.perf-card.crit { background:var(--red-s); border-color:rgba(220,38,38,0.3); }
.perf-label { font-size:12px; font-weight:700; color:var(--text3); text-transform:uppercase; letter-spacing:.04em; font-family:var(--font-mono); }
.perf-card.crit .perf-label { color:var(--red); }
.perf-main { font-size:24px; font-weight:700; letter-spacing:-.03em; color:var(--green); line-height:1; font-variant-numeric:tabular-nums; }
.perf-card.crit .perf-main { color:var(--red); }
.perf-card.warn .perf-main { color:var(--orange); }
.perf-sub { font-size:12px; color:var(--text2); font-weight:500; font-family:var(--font-mono); font-variant-numeric:tabular-nums; }
.perf-tip { display:none; position:absolute; bottom:calc(100% + 6px); left:0; right:0;
  background:var(--text1); color:var(--bg); font-size:11px; line-height:1.4;
  padding:6px 10px; border-radius:6px; z-index:10; pointer-events:none;
  box-shadow:0 4px 12px rgba(0,0,0,.15); text-align:center;
  text-transform:none; letter-spacing:0; font-family:var(--font-mono); }
.perf-card:hover .perf-tip { display:block; }
.perf-bar { height:4px; border-radius:2px; background:var(--border); overflow:hidden; margin-top:2px; }
.perf-bar-f { height:100%; border-radius:2px; background:var(--green); transition:width .6s cubic-bezier(.4,0,.2,1), background .4s; }
.perf-bar-f.warn { background:var(--orange); }
.perf-bar-f.crit { background:var(--red); }
@media (max-width:900px) { .perf-cards { grid-template-columns:repeat(3,1fr); } }
@media (max-width:600px) { .perf-cards { grid-template-columns:repeat(2,1fr); } }
.hist-card {
  background:var(--bg-card); border:1px solid var(--border);
  border-radius:var(--radius-lg); box-shadow:var(--shadow);
  overflow:hidden; transition:all .2s;
}
.hist-card:hover { box-shadow:var(--shadow-md); }
.hist-scroll { max-height:220px; overflow-y:auto; padding:14px 16px; }
.hist-scroll::-webkit-scrollbar { width:5px; }
.hist-scroll::-webkit-scrollbar-track { background:transparent; }
.hist-scroll::-webkit-scrollbar-thumb { background:var(--border-l); border-radius:2.5px; }
.hrow {
  display:grid; grid-template-columns:68px 1fr 24px 1fr 56px 24px;
  align-items:center; gap:4px; padding:8px 4px; font-size:12px;
  border-bottom:1px solid var(--bg-code); transition:background .15s; font-family:var(--font-mono);
}
.hrow:hover { background:var(--bg-code); border-radius:4px; }
.hrow:last-child { border-bottom:none; }
.hrow-hdr { font-weight:600; color:var(--text3); font-size:11px; text-transform:uppercase; letter-spacing:.04em; border-bottom:none; }
.hrow-hdr:hover { background:transparent; }
.h-time { color:var(--text3); font-variant-numeric:tabular-nums; }
.h-from { color:var(--red); font-weight:600; }
.h-arrow { color:var(--text3); text-align:center; }
.h-to { color:var(--green); font-weight:600; }
.h-dur { font-variant-numeric:tabular-nums; font-weight:600; color:var(--text1); }
.h-ok { color:var(--green); }
.h-err { color:var(--red); }
.toast {
  position:fixed; bottom:24px; right:24px; z-index:200;
  padding:10px 18px; border-radius:var(--radius-lg); font-size:13px; font-weight:600;
  font-family:var(--font-mono); transform:translateY(80px); opacity:0;
  transition:all .35s cubic-bezier(.4,0,.2,1); box-shadow:0 4px 20px rgba(0,0,0,.15); max-width:340px;
}
.toast.show { transform:translateY(0); opacity:1; }
.toast.ok { background:var(--green-s); color:var(--green); border:1px solid rgba(22,163,74,0.3); }
.toast.err { background:var(--red-s); color:var(--red); border:1px solid rgba(220,38,38,0.3); }
.toast.info { background:var(--blue-s); color:var(--blue); border:1px solid rgba(37,99,235,0.3); }
.cpu  { background:var(--orange-g); box-shadow:0 2px 8px rgba(245,158,11,.2); }
.ram  { background:var(--purple-g); box-shadow:0 2px 8px rgba(147,51,234,.2); }
.gpu  { background:var(--blue-g); box-shadow:0 2px 8px rgba(37,99,235,.2); }
.stat-icon { width:36px; height:36px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:18px; color:#fff; flex-shrink:0; background:var(--blue-g); }
.stat-body { flex:1; min-width:0; }
.stat-header { display:flex; align-items:center; gap:10px; padding:16px 20px; border-bottom:1px solid var(--border); }
.stat-header .stat-icon { background:var(--blue-g); }
.stat-header .stat-label { font-size:12px; font-weight:600; color:var(--text2); text-transform:uppercase; letter-spacing:.04em; }
.stat-header .stat-value { font-size:28px; font-weight:700; color:var(--text1); letter-spacing:-.02em; }
.stat-header .stat-unit { font-size:14px; color:var(--text3); margin-left:4px; }
.stat-header .stat-bar { flex:1; height:4px; background:var(--bg); border-radius:2px; overflow:hidden; }
.stat-header .stat-bar-f { height:100%; border-radius:2px; background:var(--blue); transition:width .6s; }
.stat-header .stat-sub { font-size:12px; color:var(--text3); margin-top:4px; }
.stat-header .stat-sub b { color:var(--text1); font-weight:600; }
.stat-body { padding:14px 20px; }
.stat-body .stat-row { display:flex; align-items:center; gap:8px; padding:6px 0; border-bottom:1px solid var(--bg); }
.stat-body .stat-row:last-child { border-bottom:none; }
.stat-body .stat-label { font-size:12px; color:var(--text2); }
.stat-body .stat-value { font-size:16px; font-weight:600; color:var(--text1); margin-left:auto; }
.cs-badge {
  display:inline-flex; align-items:center; gap:4px;
  padding:3px 10px; border-radius:8px;
  font-size:11px; font-weight:600; letter-spacing:.02em;
  background:var(--orange-s); color:var(--orange);
  margin-top:auto; align-self:flex-start;
}
.cs-card {
  background:var(--bg-card); border:1.5px dashed var(--text3);
  border-radius:var(--radius-lg); padding:20px;
  display:flex; flex-direction:column; gap:10px;
  transition:all .2s; position:relative;
}
.cs-card:hover { border-color:var(--primary); box-shadow:var(--shadow-md); transform:translateY(-1px); }
.cs-hdr { display:flex; align-items:center; gap:8px; }
.cs-icon {
  width:44px; height:44px; border-radius:12px;
  display:flex; align-items:center; justify-content:center;
  font-size:22px; color:#fff; flex-shrink:0;
  background:var(--blue-g); box-shadow:0 2px 8px rgba(37,99,235,.2);
}
.cs-icon.purple { background:var(--purple-g); box-shadow:0 2px 8px rgba(147,51,234,.2); }
.cs-icon.green  { background:var(--green-g);  box-shadow:0 2px 8px rgba(22,163,74,.2); }
.cs-title { font-size:16px; font-weight:700; color:var(--text1); letter-spacing:-.01em; }
.cs-desc { font-size:13px; color:var(--text3); line-height:1.5; }
.cs-grid {
  display:grid; grid-template-columns:repeat(2, 1fr); gap:14px;
  margin-top:14px;
}
@media (max-width:600px) { .cs-grid { grid-template-columns:1fr; } }
.tab-bar {
  display:flex; gap:2px; padding:4px; background:var(--bg-code);
  border-radius:var(--radius-lg); margin-bottom:14px;
}
.tab-item {
  flex:1; padding:8px 16px; border:none; border-radius:var(--radius);
  font-size:13px; font-weight:600; cursor:pointer;
  background:transparent; color:var(--text2); transition:all .15s;
  font-family:var(--font-mono);
}
.tab-item:hover { background:var(--bg); color:var(--text1); }
.tab-item.active { background:var(--bg-card); color:var(--text1); box-shadow:var(--shadow); }
.tab-content { display:none; }
.tab-content.active { display:block; }
.svc-chip {
  display:inline-flex; align-items:center; gap:6px;
  padding:5px 12px; border-radius:8px;
  font-size:12px; font-weight:500; cursor:pointer;
  background:var(--bg-code); color:var(--text2); transition:all .15s;
  border:1px solid var(--border);
}
.svc-chip.active { background:var(--green-s); color:var(--green); border-color:rgba(22,163,74,0.3); }
.svc-chip.inactive { background:var(--bg); color:var(--text3); border-color:var(--border); }
.svc-chip .chip-dot {
  width:6px; height:6px; border-radius:50%; background:var(--green);
}
.svc-chip.inactive .chip-dot { background:var(--text3); }
.model-desc { font-size:12px; color:var(--text2); margin-top:2px; }
.fw-group { margin-bottom:16px; }
.fw-group:last-child { margin-bottom:0; }
.fw-hdr {
  display:flex; align-items:center; gap:8px; padding:10px 14px;
  background:var(--bg-code); border-radius:var(--radius-lg);
  cursor:pointer; user-select:none; transition:all .15s;
  border:1px solid var(--border);
}
.fw-hdr:hover { background:var(--bg-card); border-color:var(--primary); }
.fw-hdr.open { margin-bottom:8px; border-radius:var(--radius-lg) var(--radius-lg) 0 0; border-bottom:none; }
.fw-chevron {
  font-size:11px; color:var(--text3); transition:transform .2s; margin-right:2px;
  font-family:var(--font-mono);
}
.fw-hdr.open .fw-chevron { transform:rotate(90deg); }
.fw-icon {
  width:28px; height:28px; border-radius:8px;
  display:flex; align-items:center; justify-content:center;
  font-size:14px; color:#fff; flex-shrink:0;
  background:var(--blue-g);
}
.fw-icon.vllm       { background:var(--red-g); }
.fw-icon.ollama     { background:var(--green-g); }
.fw-icon.ollama_cpp { background:var(--blue-g); }
.fw-icon.comfyui    { background:var(--purple-g); }
.fw-icon.webui      { background:var(--orange-g); }
.fw-label { font-size:14px; font-weight:600; color:var(--text1); font-family:var(--font-mono); }
.fw-count { font-size:12px; color:var(--text3); margin-left:4px; font-family:var(--font-mono); }
.fw-deploy-tag {
  margin-left:auto; font-size:11px; font-weight:500;
  padding:2px 8px; border-radius:6px; font-family:var(--font-mono);
}
.fw-deploy-tag.yes { background:var(--green-s); color:var(--green); }
.fw-deploy-tag.no  { background:var(--bg); color:var(--text3); }
.fw-body {
  max-height:0; overflow:hidden;
  transition:max-height .3s ease-out, padding .2s; padding:0 4px;
}
.fw-body.open { max-height:2000px; padding:0 4px; transition:max-height .5s ease-in; }
.fw-empty { font-size:13px; color:var(--text3); padding:8px 14px; font-family:var(--font-mono); }
.disc-card {
  display:flex; align-items:center; justify-content:space-between;
  padding:10px 14px; margin-bottom:6px;
  background:var(--bg-code); border:1px solid var(--border);
  border-radius:var(--radius-lg); transition:all .15s;
}
.disc-card:hover { border-color:var(--primary); background:var(--bg-card); transform:translateY(-1px); box-shadow:var(--shadow); }
.disc-card:last-child { margin-bottom:0; }
.disc-info { flex:1; min-width:0; }
.disc-name { font-size:14px; font-weight:600; color:var(--text1); font-family:var(--font-mono); }
.disc-meta { font-size:11px; color:var(--text3); margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-family:var(--font-mono); }
.disc-deploy {
  padding:5px 14px; border:1px solid var(--border); border-radius:6px;
  font-size:12px; font-weight:600; cursor:pointer;
  background:var(--green-s); color:var(--green); transition:all .15s;
  flex-shrink:0; margin-left:10px; font-family:var(--font-mono);
}
.disc-deploy:hover { background:rgba(22,163,74,.20); border-color:var(--green); }
.usage-panel {
  background:var(--bg-card); border:1px solid var(--border);
  border-radius:var(--radius-lg); box-shadow:var(--shadow);
  padding:20px; margin-bottom:18px; transition:all .2s;
}
.usage-panel:hover { box-shadow:var(--shadow-md); }
.usage-hdr { display:flex; align-items:center; gap:10px; margin-bottom:14px; }
.usage-toggle { display:flex; gap:4px; margin-left:auto; }
.usage-tab {
  padding:5px 12px; border:1px solid var(--border); border-radius:6px;
  font-size:12px; font-weight:600; cursor:pointer;
  background:var(--bg-code); color:var(--text3); transition:all .15s;
  font-family:var(--font-mono);
}
.usage-tab:hover { border-color:var(--primary); color:var(--text1); }
.usage-tab.active { background:var(--primary-s); color:var(--primary); border-color:var(--primary); }
.usage-bar-wrap {
  display:grid; grid-template-columns:140px 1fr 90px; gap:10px;
  align-items:center; padding:8px 0; border-bottom:1px solid var(--bg-code);
}
.usage-bar-wrap:last-child { border-bottom:none; }
.usage-model-name { font-size:13px; font-weight:600; color:var(--text1); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-family:var(--font-mono); }
.usage-bar-track { height:22px; border-radius:6px; background:var(--bg-code); overflow:hidden; position:relative; }
.usage-bar-f { height:100%; border-radius:6px; background:var(--blue-g); transition:width .6s cubic-bezier(.4,0,.2,1); min-width:2px; }
.usage-bar-f.alt { background:var(--green-g); }
.usage-bar-f.alt2 { background:var(--purple-g); }
.usage-bar-f.alt3 { background:var(--orange-g); }
.usage-tok-val { font-size:12px; color:var(--text2); font-variant-numeric:tabular-nums; font-family:var(--font-mono); text-align:right; }
.usage-empty { font-size:13px; color:var(--text3); padding:14px 0; text-align:center; font-family:var(--font-mono); }
.usage-legend { display:flex; gap:14px; margin-top:12px; font-size:11px; color:var(--text3); font-family:var(--font-mono); }
.usage-legend span { display:inline-flex; align-items:center; gap:5px; }
.usage-legend .dot { width:8px; height:8px; border-radius:2px; }
.gpu-panel {
  background:var(--bg-card); border:1px solid var(--border);
  border-radius:var(--radius-lg); box-shadow:var(--shadow);
  overflow:hidden; transition:all .2s;
}
.gpu-panel:hover { box-shadow:var(--shadow-md); }
.gpu-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; padding:14px; }
@media (max-width:900px) { .gpu-grid { grid-template-columns:repeat(2,1fr); } }
@media (max-width:600px) { .gpu-grid { grid-template-columns:1fr; } }
.gpu-card {
  background:var(--bg-code); border:1px solid var(--border);
  border-radius:var(--radius-lg); padding:14px; transition:all .2s;
}
.gpu-card:hover { border-color:var(--primary); }
.gpu-card-val { font-size:22px; font-weight:700; color:var(--text1); letter-spacing:-.02em; font-variant-numeric:tabular-nums; }
.gpu-card-unit { font-size:12px; color:var(--text3); margin-left:4px; }
.gpu-card-label { font-size:11px; font-weight:600; color:var(--text3); text-transform:uppercase; letter-spacing:.04em; margin-top:4px; }
.gpu-card-sub { font-size:11px; color:var(--text2); margin-top:2px; font-variant-numeric:tabular-nums; }
.gpu-card-meter { height:4px; background:var(--border); border-radius:2px; margin-top:8px; overflow:hidden; }
.gpu-card-meter-f { height:100%; border-radius:2px; background:var(--green); transition:width .6s; }
.gpu-card-meter-f.warn { background:var(--orange); }
.gpu-card-meter-f.crit { background:var(--red); }
.gpu-card-header { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
.gpu-card-dot { width:10px; height:10px; border-radius:50%; }
.gpu-card-dot.ok { background:var(--green); }
.gpu-card-dot.warn { background:var(--orange); }
.gpu-card-dot.crit { background:var(--red); }
.gpu-card-name { font-size:13px; font-weight:600; color:var(--text1); font-family:var(--font-mono); }
.gpu-card-body { display:flex; flex-direction:column; gap:4px; }
.gpu-card-row { display:flex; justify-content:space-between; align-items:center; }
.disc-models-scroll { max-height:360px; overflow-y:auto; }
.disc-models-scroll::-webkit-scrollbar { width:5px; }
.disc-models-scroll::-webkit-scrollbar-track { background:transparent; }
.disc-models-scroll::-webkit-scrollbar-thumb { background:var(--border-l); border-radius:2.5px; }
.disc-model-card {
  display:flex; align-items:center; gap:8px; height:48px;
  padding:0 10px; margin-bottom:4px;
  background:var(--bg-code); border:1px solid var(--border);
  border-radius:8px; transition:all .15s;
}
.disc-model-card:hover { border-color:var(--primary); background:var(--bg-card); }
.disc-model-card:last-child { margin-bottom:0; }
.disc-model-name { font-size:13px; font-weight:600; color:var(--text1); font-family:var(--font-mono); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; flex-shrink:0; max-width:180px; }
.disc-model-tag {
  display:inline-flex; align-items:center; gap:3px; padding:2px 8px; border-radius:4px;
  font-size:10px; font-weight:600; font-family:var(--font-mono); white-space:nowrap; flex-shrink:0;
}
.disc-model-tag.vllm { background:var(--red-s); color:var(--red); }
.disc-model-tag.ollama { background:var(--green-s); color:var(--green); }
.disc-model-tag.ollama_cpp { background:var(--blue-s); color:var(--blue); }
.disc-model-tag.comfyui { background:var(--purple-s); color:var(--purple); }
.disc-model-tag.webui { background:var(--orange-s); color:var(--orange); }
.disc-model-size { font-size:11px; color:var(--text3); font-family:var(--font-mono); white-space:nowrap; flex-shrink:0; font-variant-numeric:tabular-nums; }
.disc-model-status {
  display:inline-flex; align-items:center; gap:3px; padding:2px 8px; border-radius:4px;
  font-size:10px; font-weight:600; font-family:var(--font-mono); white-space:nowrap; margin-left:auto;
}
.disc-model-status.deployed { background:var(--green-s); color:var(--green); }
.disc-model-status.undeployed { background:var(--bg); color:var(--text3); border:1px solid var(--border); }
.disc-model-actions { display:flex; gap:4px; flex-shrink:0; }
.disc-model-btn {
  padding:3px 10px; border:1px solid var(--border); border-radius:5px;
  font-size:11px; font-weight:600; cursor:pointer; transition:all .15s;
  font-family:var(--font-mono); white-space:nowrap; font-variant-numeric:tabular-nums;
}
.disc-model-btn.deploy { background:var(--green-s); color:var(--green); }
.disc-model-btn.deploy:hover { background:rgba(22,163,74,.20); border-color:var(--green); }
.disc-model-btn.pull { background:var(--primary-s); color:var(--primary); }
.disc-model-btn.pull:hover { background:rgba(204,122,96,.20); border-color:var(--primary); }
.disc-empty { font-size:13px; color:var(--text3); padding:10px 0; text-align:center; font-family:var(--font-mono); }
.deploy-form-field { margin-bottom:10px; }
.deploy-form-label { display:block; font-size:12px; font-weight:600; color:var(--text2); margin-bottom:4px; font-family:var(--font-mono); }
.deploy-form-input {
  width:100%; padding:6px 10px; border:1px solid var(--border); border-radius:6px;
  font-size:13px; font-family:var(--font-mono); color:var(--text1); background:var(--bg);
  outline:none; transition:border-color .15s; font-variant-numeric:tabular-nums;
}
.deploy-form-input:focus { border-color:var(--primary); }
.deploy-form-slider {
  width:100%; height:6px; -webkit-appearance:none; appearance:none;
  background:var(--bg-code); border-radius:3px; outline:none; cursor:pointer;
}
.deploy-form-slider::-webkit-slider-thumb {
  -webkit-appearance:none; width:16px; height:16px; border-radius:50%;
  background:var(--primary); cursor:pointer; border:2px solid #fff; box-shadow:0 1px 3px rgba(0,0,0,.2);
}
.deploy-form-slider::-moz-range-thumb {
  width:16px; height:16px; border-radius:50%;
  background:var(--primary); cursor:pointer; border:2px solid #fff; box-shadow:0 1px 3px rgba(0,0,0,.2);
}
.deploy-form-select {
  width:100%; padding:6px 10px; border:1px solid var(--border); border-radius:6px;
  font-size:13px; font-family:var(--font-mono); color:var(--text1); background:var(--bg);
  outline:none; cursor:pointer; transition:border-color .15s; font-variant-numeric:tabular-nums;
}
.deploy-form-select:focus { border-color:var(--primary); }
.deploy-form-btn {
  width:100%; padding:8px 0; border:none; border-radius:6px;
  font-size:13px; font-weight:600; cursor:pointer; transition:all .15s;
  font-family:var(--font-mono); color:#fff; margin-top:6px;
}
.deploy-form-btn.vllm { background:var(--red-g); box-shadow:0 2px 6px rgba(220,38,38,.2); }
.deploy-form-btn.vllm:hover { filter:brightness(1.1); }
.deploy-form-btn.ollama { background:var(--green-g); box-shadow:0 2px 6px rgba(22,163,74,.2); }
.deploy-form-btn.ollama:hover { filter:brightness(1.1); }
</style>
</head>
<body>

<div class="nav">
  <div class="nav-in">
    <div class="nav-l">
      <div class="nav-logo">E</div>
      <span class="nav-title">InferFabric</span>
      <span class="tag idle" id="sTag"><span class="dot"></span><span id="sTxt">idle</span></span>
    </div>
    <div class="nav-r">
      <span class="nav-ver">v4.3</span>
      <span class="nav-ts" id="ts">—</span>
    </div>
  </div>
</div>

<div class="main">

  <!-- Status Cards -->
  <div class="status-bar">
    <div class="stat-card">
      <div class="stat-icon gpu">💾</div>
      <div class="stat-body">
        <div class="stat-label">GPU 显存</div>
        <div class="stat-row">
          <span class="stat-val" id="gP">0</span><span class="stat-unit">%</span>
        </div>
        <div class="stat-bar"><div class="stat-bar-f" id="gB" style="width:0%;background:var(--blue)"></div></div>
        <div class="stat-sub"><b id="gU">0</b> / <span id="gT">32,607</span> MB</div>
      </div>
    </div>
    <div class="stat-card">
      <div class="stat-icon" style="background:var(--red-g);box-shadow:0 2px 8px rgba(255,69,58,.2)">🔥</div>
      <div class="stat-body">
        <div class="stat-label">GPU 负载</div>
        <div class="stat-row">
          <span class="stat-val" id="guP">0</span><span class="stat-unit">%</span>
        </div>
        <div class="stat-bar"><div class="stat-bar-f" id="guB" style="width:0%;background:var(--red)"></div></div>
        <div class="stat-sub"><span id="guC">—</span> MHz · <span id="guW">—</span> W</div>
      </div>
    </div>
    <div class="stat-card">
      <div class="stat-icon ram">🧠</div>
      <div class="stat-body">
        <div class="stat-label">系统内存</div>
        <div class="stat-row">
          <span class="stat-val" id="rP">0</span><span class="stat-unit">%</span>
        </div>
        <div class="stat-bar"><div class="stat-bar-f" id="rB" style="width:0%;background:var(--purple)"></div></div>
        <div class="stat-sub"><b id="rU">0</b> / <span id="rT">—</span> GB</div>
      </div>
    </div>
    <div class="stat-card">
      <div class="stat-icon cpu">⚡</div>
      <div class="stat-body">
        <div class="stat-label">CPU 负载</div>
        <div class="stat-row">
          <span class="stat-val" id="cP">0</span><span class="stat-unit">%</span>
        </div>
        <div class="stat-bar"><div class="stat-bar-f" id="cB" style="width:0%;background:var(--orange)"></div></div>
        <div class="stat-sub"><span id="cC">—</span> 核心 · 运行 <span id="cU">—</span></div>
      </div>
    </div>
  </div>

  <!-- Tab Bar -->
  <div class="tab-bar">
    <span class="tab-item active" data-tab="tab-inference" onclick="switchTab('tab-inference')">🔄 模型推理</span>
    <span class="tab-item" data-tab="tab-monitor" onclick="switchTab('tab-monitor')">📊 指标监控</span>
    <span class="tab-item" data-tab="tab-deploy" onclick="switchTab('tab-deploy')">📦 模型部署</span>
  </div>

  <!-- ════════ Tab 1: 模型推理 (default active) ════════ -->
  <div class="tab-content active" id="tab-inference">

  <!-- Active Services -->
  <div class="panel" id="svcCard" style="margin-bottom:18px;padding:16px 20px;min-height:40px"></div>

  <!-- Model Panels -->
  <div class="panels">
    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-icon excl">🔒</div>
        <span class="panel-title">独占模型</span>
      </div>
      <div id="exclList" class="model-grid"></div>
    </div>
    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-icon shrd">🔓</div>
        <span class="panel-title">共享服务</span>
      </div>
      <div id="shrdList" class="model-grid"></div>
    </div>
    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-icon free">⚡</div>
        <span class="panel-title">空闲</span>
      </div>
      <div id="freeList" class="model-grid"></div>
    </div>
  </div>

  <div class="act-row">
    <button class="act-btn pri" onclick="doSwitch('idle')">释放 GPU</button>
    <button class="act-btn sec" onclick="doReconcile()">Reconcile</button>
    <button class="act-btn warn" onclick="doReset()">强制重置</button>
  </div>

  </div><!-- /tab-inference -->

  <!-- ════════ Tab 2: 指标监控 ════════ -->
  <div class="tab-content" id="tab-monitor">

  <!-- vLLM Perf Panel -->
  <div class="perf-panel" id="perfPanel" style="display:none">
    <div class="perf-hdr">
      <div class="panel-icon" style="background:var(--teal);box-shadow:0 2px 6px rgba(100,210,255,.2)">📊</div>
      <span class="panel-title" id="perfTitle">vLLM 性能</span>
      <span class="perf-sleep-badge" id="sleepBadge">休眠中</span>
      <span class="perf-refresh">刷新 60s</span>
    </div>
    <div class="perf-cards">
      <div class="perf-card" id="pcKv">
        <span class="perf-label">KV Cache</span>
        <span class="perf-tip">GPU KV 缓存占用率。>90% 容易触发抢占，导致延迟飙升</span>
        <span class="perf-main" id="kvVal">—</span>
        <div class="perf-bar"><div class="perf-bar-f" id="kvBar" style="width:0"></div></div>
      </div>
      <div class="perf-card" id="pcSeq">
        <span class="perf-label">Seq Length</span>
        <span class="perf-tip">平均请求长度（Prompt + Generation 总 Token 数）。反映单次请求的上下文规模</span>
        <span class="perf-main" id="seqVal">—</span>
        <span class="perf-sub" id="seqSub"></span>
      </div>
      <div class="perf-card" id="pcTpot">
        <span class="perf-label">TPOT</span>
        <span class="perf-tip">Time Per Output Token。生成单个 token 的平均耗时（秒）。越低越好，<50ms 优秀</span>
        <span class="perf-main" id="tpotVal">—</span>
        <span class="perf-sub" id="tpotSub"></span>
      </div>
      <div class="perf-card" id="pcTtft">
        <span class="perf-label">TTFT</span>
        <span class="perf-tip">Time to First Token。从发送请求到收到第一个字的延迟（秒）。<1s 优秀</span>
        <span class="perf-main" id="tfVal">—</span>
        <span class="perf-sub" id="tfSub"></span>
      </div>
      <div class="perf-card" id="pcTput">
        <span class="perf-label">Throughput</span>
        <span class="perf-tip">生成吞吐量（tokens/s）。1 / 平均 inter-token 延迟。数值越高，生成速度越快</span>
        <span class="perf-main" id="tpVal">—</span>
        <span class="perf-sub" id="tpSub"></span>
      </div>
    </div>
  </div>

  <!-- Usage Chart (P1) -->
  <div class="usage-panel" id="usagePanel">
    <div class="usage-hdr">
      <div class="panel-icon" style="background:var(--green-g);box-shadow:0 2px 6px rgba(48,209,88,.2)">📈</div>
      <span class="panel-title">Token 用量</span>
      <div class="usage-toggle">
        <button class="usage-tab" data-w="daily">24h</button>
        <button class="usage-tab active" data-w="weekly">7d</button>
        <button class="usage-tab" data-w="monthly">1m</button>
        <button class="usage-tab" data-w="all">全部</button>
      </div>
    </div>
    <div id="usageBody"><div class="usage-empty">加载中…</div></div>
    <div class="usage-legend">
      <span><span class="dot" style="background:var(--blue-g)"></span>tokens</span>
      <span id="usageTotal" style="margin-left:auto"></span>
    </div>
  </div>

  <!-- GPU Metrics (P1) -->
  <div class="gpu-panel" id="gpuPanel">
    <div class="panel-hdr">
      <div class="panel-icon" style="background:var(--blue-g);box-shadow:0 2px 6px rgba(10,132,255,.2)">📊</div>
      <span class="panel-title">GPU 实时指标</span>
      <span style="margin-left:auto;font-size:11px;color:var(--text3)" id="gpuTs">—</span>
    </div>
    <div class="gpu-grid" id="gpuGrid"></div>
  </div>

  <!-- History -->
  <div class="hist-card">
    <div class="panel-hdr">
      <div class="panel-icon" style="background:var(--purple-g);box-shadow:0 2px 6px rgba(191,90,242,.2)">🕐</div>
      <span class="panel-title">切换历史</span>
    </div>
    <div class="hist-scroll">
      <div class="hrow hrow-hdr"><span>时间</span><span>来源</span><span></span><span>目标</span><span>耗时</span><span></span></div>
      <div id="hBody"><div style="text-align:center;padding:20px;color:var(--text4);font-size:14px">加载中…</div></div>
    </div>
  </div>
  </div><!-- /tab-monitor -->

  <!-- ════════ Tab 3: 模型部署 ════════ -->
  <div class="tab-content" id="tab-deploy">
    <div class="panel" id="localModels" style="display:none">
      <div class="panel-hdr" style="padding:6px 12px">
        <div class="panel-icon" style="background:var(--blue-g);box-shadow:0 2px 6px rgba(10,132,255,.2);width:22px;height:22px;font-size:11px">📦</div>
        <span class="panel-title" style="font-size:13px">本地模型发现</span>
      </div>
      <div class="panel-content" style="padding:6px 10px" id="localModelsList"></div>
    </div>

    <!-- Deploy form toggle -->
    <button class="act-btn sec" onclick="toggleDeployForm()" id="deployFormToggle" style="margin-bottom:14px">展开部署表单</button>
    <div id="deployFormArea" style="display:none">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <!-- vLLM Deploy Form -->
        <div class="panel">
          <div class="panel-hdr">
            <div class="panel-icon vllm">🔥</div>
            <span class="panel-title">vLLM 部署</span>
          </div>
          <div class="panel-content" style="padding:12px 14px">
            <form id="vllmDeployForm" onsubmit="return submitVllmDeploy(event)">
              <div class="deploy-form-field">
                <label class="deploy-form-label">模型名称</label>
                <input class="deploy-form-input" name="name" type="text" placeholder="例: Qwen2.5-7B" required>
              </div>
              <div class="deploy-form-field">
                <label class="deploy-form-label">模型目录</label>
                <input class="deploy-form-input" name="model_dir" type="text" value="~/models/">
              </div>
              <div class="deploy-form-field">
                <label class="deploy-form-label">服务端口</label>
                <input class="deploy-form-input" name="port" type="number" placeholder="自动分配" min="1024" max="65535">
              </div>
              <div class="deploy-form-field">
                <label class="deploy-form-label">GPU 显存利用率 <span id="vllmGpuMemVal">0.80</span></label>
                <input class="deploy-form-slider" name="gpu_mem" type="range" min="0.50" max="0.95" step="0.01" value="0.80" oninput="document.getElementById('vllmGpuMemVal').textContent=parseFloat(this.value).toFixed(2)">
              </div>
              <div class="deploy-form-field">
                <label class="deploy-form-label">最大上下文长度</label>
                <input class="deploy-form-input" name="max_ctx" type="number" value="4096" min="512" max="131072">
              </div>
              <div class="deploy-form-field">
                <label class="deploy-form-label">量化格式</label>
                <select class="deploy-form-select" name="quantization">
                  <option value="">无</option>
                  <option value="NVFP4">NVFP4</option>
                  <option value="GPTQ-4bit">GPTQ-4bit</option>
                  <option value="Q8_0">Q8_0</option>
                </select>
              </div>
              <div class="deploy-form-field">
                <label class="deploy-form-label">推理模式</label>
                <select class="deploy-form-select" name="inference_mode">
                  <option value="auto">auto</option>
                  <option value="eager">eager</option>
                </select>
              </div>
              <button type="submit" class="deploy-form-btn vllm">部署 vLLM 模型</button>
            </form>
          </div>
        </div>
        <!-- Ollama Deploy Form -->
        <div class="panel">
          <div class="panel-hdr">
            <div class="panel-icon ollama">🦙</div>
            <span class="panel-title">Ollama 部署</span>
          </div>
          <div class="panel-content" style="padding:12px 14px">
            <form id="ollamaDeployForm" onsubmit="return submitOllamaDeploy(event)">
              <div class="deploy-form-field">
                <label class="deploy-form-label">模型名称</label>
                <input class="deploy-form-input" name="name" type="text" placeholder="例: llama3" required>
              </div>
              <div class="deploy-form-field">
                <label class="deploy-form-label">Ollama 模型引用</label>
                <input class="deploy-form-input" name="ollama_ref" type="text" placeholder="例: llama3.2:3b">
              </div>
              <div class="deploy-form-field">
                <label class="deploy-form-label">服务端口（ollama.cpp）</label>
                <input class="deploy-form-input" name="port" type="number" placeholder="自动分配" min="1024" max="65535">
              </div>
              <div class="deploy-form-field">
                <label class="deploy-form-label">GPU 卸载层数</label>
                <input class="deploy-form-input" name="gpu_layers" type="number" value="-1" min="-1" max="200">
                <div style="font-size:11px;color:var(--text3);margin-top:4px">-1 = 全部卸载到 GPU</div>
              </div>
              <button type="submit" class="deploy-form-btn ollama">部署 Ollama 模型</button>
            </form>
          </div>
        </div>
      </div>
    </div>
  </div><!-- /tab-deploy -->

<div class="toast" id="toast"></div>

<script>
function swLock() {
  // Cross-tab lock via localStorage (shared across tabs)
  try {
    const key = 'inferfabric_sw_lock';
    const swT=Date.now();
    const stored = localStorage.getItem(key);
    if (stored && (swT - parseInt(stored, 10)) < 30000) return false;
    localStorage.setItem(key, String(swT));
    sw=true;
    return true;
  } catch(e) { return true; } // localStorage unavailable → allow
}
function swUnlock() {
  try { localStorage.removeItem('inferfabric_sw_lock'); } catch(e) {}
}
function switchTab(tabId) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-item').forEach(t => t.classList.remove('active'));
  const target = document.getElementById(tabId);
  if (target) target.classList.add('active');
  const btn = document.querySelector(`.tab-item[data-tab="${tabId}"]`);
  if (btn) btn.classList.add('active');
}
// Cleanup on tab close
window.addEventListener('beforeunload', swUnlock);
function toast(m,t) {
  const e=document.getElementById('toast');
  e.textContent=m; e.className='toast '+t+' show';
  clearTimeout(e._t); e._t=setTimeout(()=>e.classList.remove('show'),2800);
}
async function j(p,o) { return (await fetch(p,o)).json(); }

async function load() {
  const [s,sys,hist]=await Promise.all([j('/status'),j('/system').catch(()=>({})),j('/history').catch(()=>[])]);
  const gm=s.gpu_mode||'idle';
  const labels={idle:'idle',exclusive:'exclusive',shared:'shared'};
  const tag=document.getElementById('sTag');
  tag.className='tag '+gm;
  document.getElementById('sTxt').textContent=labels[gm]||gm;

  // GPU
  const gt=s.gpu_total_mb||32607,gu=s.gpu_used_mb||0,gp=(gu/gt*100);
  document.getElementById('gP').textContent=gp.toFixed(1);
  document.getElementById('gU').textContent=gu.toLocaleString();
  document.getElementById('gT').textContent=gt.toLocaleString();
  document.getElementById('gB').style.width=gp.toFixed(1)+'%';
  document.getElementById('gB').style.background=gp<50?'var(--blue)':gp<80?'var(--orange)':'var(--red)';

  // GPU Load
  const guP=sys.gpu_util_pct||0;
  document.getElementById('guP').textContent=guP.toFixed(1);
  document.getElementById('guB').style.width=guP.toFixed(1)+'%';
  document.getElementById('guB').style.background=guP<30?'var(--green)':guP<70?'var(--orange)':'var(--red)';
  document.getElementById('guC').textContent=sys.gpu_clock_mhz||'—';
  document.getElementById('guW').textContent=sys.gpu_power_w||'—';

  // RAM
  const rt=sys.ram_total_gb||1,ru=sys.ram_used_gb||0,rp=(ru/rt*100);
  document.getElementById('rP').textContent=rp.toFixed(1);
  document.getElementById('rU').textContent=ru.toFixed(1);
  document.getElementById('rT').textContent=rt.toFixed(1);
  document.getElementById('rB').style.width=rp.toFixed(1)+'%';

  // CPU
  const cp=sys.cpu_percent||0;
  document.getElementById('cP').textContent=cp.toFixed(1);
  document.getElementById('cC').textContent=sys.cpu_cores||'—';
  document.getElementById('cB').style.width=cp.toFixed(1)+'%';
  const us=sys.uptime_seconds||0;
  document.getElementById('cU').textContent=Math.floor(us/3600)+'h '+Math.floor((us%3600)/60)+'m';

  // History
  const hBody=document.getElementById('hBody');
  if(!hist||!hist.length){hBody.innerHTML='<div style="text-align:center;padding:20px;color:var(--text4);font-size:14px">暂无记录</div>';}
  else{hBody.innerHTML=hist.slice(0,12).map(h=>{
    const t=h.timestamp?new Date(h.timestamp+'Z'):new Date();
    const ts=t.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    const d=h.duration!=null?h.duration.toFixed(1)+'s':'—';
    const st=h.status==='ok'?'<span class="h-ok">✓</span>':'<span class="h-err">✗</span>';
    return '<div class="hrow"><span class="h-time">'+ts+'</span><span class="h-from">'+(h.from||'—')+'</span><span class="h-arrow">→</span><span class="h-to">'+h.to+'</span><span class="h-dur">'+d+'</span><span>'+st+'</span></div>';
  }).join('');}

  document.getElementById('ts').textContent=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});

  // vLLM metrics: detect active vLLM service from services_info
  const svcInfo=s.services_info||{};
  let vPort=null, vName=null;
  for(const n of (s.active_services||[])){
    const info=svcInfo[n]||{};
    if(info.type==='vllm'&&info.port){ vPort=info.port; vName=n; break; }
  }
  if(vPort){
    clearInterval(vllmTimer);
    loadVllmMetrics(vPort,vName);
    vllmTimer=setInterval(()=>loadVllmMetrics(vPort,vName),10000);
  }else{
    document.getElementById('perfPanel').style.display='none';
    clearInterval(vllmTimer);
    vllmTimer=null;
  }

  // Active services row layout
  const svcs=s.active_services||[];
  const health=s.services_health||{};
  const sInfo=s.services_info||{};
  const svcCard=document.getElementById('svcCard');
  let svcHtml='<div style="display:flex;align-items:center;gap:10px;margin-bottom:'+(svcs.length?12:0)+'px"><div class="panel-icon" style="background:var(--blue-g);box-shadow:0 2px 6px rgba(10,132,255,.2)">📡</div><span class="panel-title">活跃服务</span></div>';
  if(svcs.length===0){
    svcHtml+='<span class="svc-empty">无活跃服务</span>';
  }else{
    for(const n of svcs){
      const h=health[n]||"❌";
      const ok=h==='✅';
      const info=sInfo[n]||{};
      const port=info.port||'—';
      const mode=info.mode||'?';
      const modeTag=mode==='exclusive'?'<span class="model-badge excl" style="padding:2px 8px;font-size:10px">独占</span>':'<span class="model-badge shrd" style="padding:2px 8px;font-size:10px">共享</span>';
      const sleepMatch=h.match(/sleeping [A-Z0-9]+/);
      const sleepLabel=sleepMatch?' <span style="color:var(--purple);font-size:11px;font-weight:500">⏸ '+sleepMatch[0]+'</span>':'';
      svcHtml+='<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--bg)">';
      svcHtml+='<span style="color:'+(ok?'var(--green)':'var(--red)')+';font-size:16px">'+(ok?'✓':'✗')+'</span>';
      svcHtml+='<span style="flex:1;font-size:14px;font-weight:600;color:var(--text1)">'+n+'</span>';
      svcHtml+='<span style="font-size:12px;color:var(--text3);font-variant-numeric:tabular-nums">:'+port+'</span>';
      svcHtml+=modeTag+sleepLabel+'</div>';
    }
  }
  svcCard.innerHTML=svcHtml;
}

// ── vLLM Performance ──
let _tput={pt:0,gt:0,ts:0};
let vllmTimer=null;

async function loadVllmMetrics(port,modelName) {
  const panel=document.getElementById('perfPanel');
  try {
    const m=await j('/vllm_metrics?port='+port);
    if(m.error){ panel.style.display='none'; return; }
    panel.style.display='';
    document.getElementById('perfTitle').textContent=modelName+' 性能';

    const sleeping=m.sleep_state===1;
    panel.className='perf-panel'+(sleeping?' sleeping':'');
    document.getElementById('sleepBadge').style.display=sleeping?'inline-block':'none';

    // KV Cache
    const kv=m.kv_cache_usage_perc??0;
    const kvCls=kv>90?'crit':kv>70?'warn':'';
    document.getElementById('kvVal').textContent=kv.toFixed(1)+'%';
    document.getElementById('kvBar').style.width=kv.toFixed(1)+'%';
    document.getElementById('kvBar').className='perf-bar-f'+(kvCls?' '+kvCls:'');
    document.getElementById('pcKv').className='perf-card'+(kvCls?' '+kvCls:'');

    // Seq Length
    if(m.seq_length!=null) {
      document.getElementById('seqVal').textContent=m.seq_length.toLocaleString()+' tokens';
      document.getElementById('seqSub').textContent='P '+m.seq_prompt?.toLocaleString()+' + G '+m.seq_generation?.toLocaleString()+' ('+m.seq_count+' requests)';
    } else {
      document.getElementById('seqVal').textContent='—';
      document.getElementById('seqSub').textContent='';
    }

    // TPOT
    const tpot=m.tpot_seconds||{};
    if(m.tpot_cum_mean!=null) {
      const tpotMs=m.tpot_cum_mean*1000;
      document.getElementById('tpotVal').textContent=tpotMs.toFixed(1)+' ms';
      document.getElementById('tpotSub').textContent='P50 '+(tpot.p50*1000).toFixed(1)+'ms | P95 '+(tpot.p95*1000).toFixed(1)+'ms | '+m.tpot_cum_n+' reqs';
    } else {
      document.getElementById('tpotVal').textContent='—';
      document.getElementById('tpotSub').textContent='';
    }

    // TTFT — 运行期间平均值 (排除零值)
    const tf=m.ttft_seconds||{};
    if(m.ttft_cum_mean!=null) {
      document.getElementById('tfVal').textContent=m.ttft_cum_mean.toFixed(2)+'s';
      document.getElementById('tfSub').textContent='累计 '+m.ttft_cum_n+' 次 | P50 '+tf.p50?.toFixed(2)+'s | P95 '+tf.p95?.toFixed(2)+'s';
    } else if(tf.mean!=null) {
      document.getElementById('tfVal').textContent=tf.mean.toFixed(2)+'s';
      document.getElementById('tfSub').textContent='P50 '+tf.p50?.toFixed(2)+'s | P95 '+tf.p95?.toFixed(2)+'s | '+tf.count+' 次';
    } else {
      document.getElementById('tfVal').textContent='—';
      document.getElementById('tfSub').textContent='';
    }

    // Throughput — EMA (active-only), excludes idle time
    // Primary: smoothed average over last 30-40s of active generation
    // Sub: instant 10s sample + total tokens
    if(m.throughput!=null) {
      document.getElementById('tpVal').textContent=m.throughput+' t/s (EMA)';
      var subText = 'total '+m.throughput_cum_n?.toLocaleString()+' tokens';
      if(m.throughput_inst!==undefined && m.throughput_inst!==null)
        subText += ' | '+m.throughput_inst+' t/s (10s)';
      document.getElementById('tpSub').textContent=subText;
    } else {
      document.getElementById('tpVal').textContent='—';
      document.getElementById('tpSub').textContent='';
    }
  }catch(e){ panel.style.display='none'; }
}

async function loadModels() {
  const [models, st] = await Promise.all([j('/models'), j('/status')]);
  const excl=models.filter(m=>m.mode==='exclusive');
  const shrd=models.filter(m=>m.mode==='shared');
  const free=models.filter(m=>m.mode==='none');
  const sleepSt=st.sleep_states||{};
  const svcInfo=st.services_info||{};

  function renderCard(m, modeBadge) {
    const isVllm=m.type==='vllm';
    const info=svcInfo[m.name]||{};
    const port=info.port||'—';
    const sleeping=!!sleepSt[m.name];
    const active=m.active&&!sleeping;
    const cls='model-card'+(active?' active':'');

    let statusLine='<span style="color:var(--text4);font-size:12px">○ stopped</span>';
    if(active) statusLine='<span style="color:var(--green);font-size:12px;font-weight:600">✅ running</span>';
    else if(sleeping) statusLine='<span style="color:var(--purple);font-size:12px;font-weight:600">⏸ sleeping</span>';

    let btns='';
    if(active){
      btns+='<button class="btn-card stop" onclick="event.stopPropagation();doRelease(\''+m.name+'\','+(m.mode==='exclusive')+')">释放</button>';
      if(isVllm) btns+='<button class="btn-card start" onclick="event.stopPropagation();doSleep(\''+m.name+'\')">休眠</button>';
    }else if(sleeping){
      btns+='<button class="btn-card stop" onclick="event.stopPropagation();doRelease(\''+m.name+'\','+(m.mode==='exclusive')+')">释放</button>';
      if(isVllm) btns+='<button class="btn-card start" onclick="event.stopPropagation();doWake(\''+m.name+'\')">唤醒</button>';
    }else{
      btns+='<button class="btn-card start" onclick="event.stopPropagation();doSwitch(\''+m.name+'\')">启动</button>';
    }

    // Specs row: framework + model type + context window + quantization
    const fwIcons = { vllm:'🔥', ollama:'🦙', ollama_cpp:'📦', comfyui:'🖼️' };
    const fwLabels = { vllm:'vLLM', ollama:'Ollama', ollama_cpp:'ollama.cpp', comfyui:'ComfyUI' };
    const framework = fwLabels[m.type] || m.type;
    const fwIcon = fwIcons[m.type] || '📦';
    const ctxStr = m.context_window ? (m.context_window >= 1024 ? (m.context_window/1024).toFixed(0)+'K ctx' : m.context_window+' ctx') : '';
    const typeIcon = { text:'🧠', 'text-vision':'👁', multimodal:'🌐', aigc:'✨', embedding:'📊' };
    const typeLabel = { text:'LLM', 'text-vision':'VL', multimodal:'Omni', aigc:'AIGC', embedding:'Embedding' };
    // badge 文案：modeBadge 取值 excl/shrd/free，对应独占/共享/空闲
    const modeLabel = { excl:'独占', shrd:'共享', free:'空闲' };
    // Legacy model_type → modality mapping for backward compatibility
    const legacyModality = { llm:'text', vl:'text-vision', omni:'multimodal', aigc:'aigc', multimodal:'multimodal' };
    const modality = m.modality ?? legacyModality[m.model_type] ?? 'text';
    let specs = '<span class="spec-tag">'+fwIcon+' '+framework+'</span>';
    // Always show modality tag for consistent card layout
    if(typeIcon[modality]) specs += '<span class="spec-tag">'+typeIcon[modality]+' '+(typeLabel[modality]||modality)+'</span>';
    if(ctxStr) specs += '<span class="spec-tag">📐 '+ctxStr+'</span>';
    if(m.quantization) specs += '<span class="spec-tag">⚡ '+m.quantization+'</span>';

    return '<div class="'+cls+'" id="sw-'+m.name+'">'+
      '<div class="model-top">'+
        '<div class="model-dot"></div>'+
        '<div class="model-info"><div class="model-name">'+m.name+'</div>'+
          '<div style="font-size:11px;color:var(--text3);margin-top:3px">'+statusLine+' <span style="margin-left:6px;font-variant-numeric:tabular-nums;color:var(--text4)">:'+port+'</span></div>'+
        '</div>'+
        '<span class="model-badge '+modeBadge+'">'+(modeLabel[modeBadge]||modeBadge)+'</span>'+
      '</div>'+
      '<div class="model-specs">'+specs+'</div>'+
      '<div class="model-actions">'+btns+'</div>'+
    '</div>';
  }

  document.getElementById('exclList').innerHTML=excl.map(m=>renderCard(m,'excl')).join('');
  document.getElementById('shrdList').innerHTML=shrd.map(m=>renderCard(m,'shrd')).join('');
  document.getElementById('freeList').innerHTML=free.length>0?free.map(m=>renderCard(m,'free')).join(''):'<div class="fill">⚡ 无模型</div>';
}

async function doRelease(n,isExcl) {
  if(!swLock())return;
  try{
    // P0-3: For shared models, stop then check if idle needed
    if(isExcl) {
      const r = await j('/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:'idle'})});
      if(r.status==='switched') toast(n+' 已释放','ok');
      else toast(r.message||'失败','err');
    } else {
      const r = await j('/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
      if(r.status==='stopped') {
        // P0-3: Check if we should transition to idle
        const status = await j('/status');
        if(status.active_services && status.active_services.length === 0) {
          await j('/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:'idle'})});
          toast(n+' 已释放 → idle','ok');
        } else {
          toast(n+' 已释放','ok');
        }
      } else {
        toast(r.message||'失败','err');
      }
    }
  }catch(e){toast(e.message,'err');}
  finally{sw=false;}
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

async function doSleep(n) {
  if(!swLock())return;
  try{
    const r=await j('/sleep',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='ok') toast(n+' 休眠 ✓','ok');
    else if(r.status==='already_sleeping') toast(n+' 已在休眠','info');
    else toast(r.message||'失败','err');
  }catch(e){toast(e.message,'err');}
  finally{sw=false;}
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

async function doWake(n) {
  if(!swLock())return;
  try{
    const r=await j('/wake',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='ok') toast(n+' 已唤醒 ✓','ok');
    else if(r.status==='already_awake') toast(n+' 未休眠','info');
    else toast(r.message||'失败','err');
  }catch(e){toast(e.message,'err');}
  finally{sw=false;}
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

async function doSwitch(n) {
  if(!swLock())return;
  try{
    const r=await j('/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='switched') toast(n+' ✓ '+r.elapsed_sec+'s','ok');
    else if(r.status==='config_changed_restart') toast('配置已变更，'+n+' 正在重启','ok');
    else if(r.status==='already_active') toast('已在 '+n,'info');
    else toast(r.message||'失败','err');
  }catch(e){toast(e.message,'err');}
  finally{sw=false;}
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

async function doStop(n) {
  if(!swLock())return;
  try{
    const r=await j('/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='stopped') toast(n+' 已停止','ok');
    else if(r.status==='already_stopped') toast(n+' 未运行','info');
    else toast(r.message||'停止失败','err');
  }catch(e){toast(e.message,'err');}
  finally{sw=false;}
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

async function loadLocalModels() {
  try {
    const [d, st] = await Promise.all([j('/local-models'), j('/status')]);
    const list = d.discovered || [];
    const el = document.getElementById('localModels');
    const listEl = document.getElementById('localModelsList');

    // Active services names for deploy-status check
    const activeNames = new Set(st.active_services || []);

    // Framework metadata
    const fwMeta = {
      vllm:      { icon: '\uD83D\uDD25', label: 'vLLM',        canDeploy: true  },
      ollama:     { icon: '\uD83E\uDD99', label: 'Ollama',      canDeploy: true  },
      ollama_cpp: { icon: '\uD83D\uDCE6', label: 'ollama.cpp',   canDeploy: true  },
      comfyui:    { icon: '\uD83C\uDFA8', label: 'ComfyUI',     canDeploy: false },
      webui:      { icon: '\uD83C\uDF10', label: 'Web UI',      canDeploy: false },
    };
    const fwOrder = ['vllm', 'ollama', 'ollama_cpp', 'comfyui', 'webui'];

    // Group by framework
    const groups = {};
    for (const m of list) {
      const fw = m.framework || 'other';
      if (!groups[fw]) groups[fw] = [];
      groups[fw].push(m);
    }

    const totalCount = list.length;

    // Always show panel
    el.style.display = 'block';

    if (totalCount === 0) {
      listEl.innerHTML = '<div class="disc-empty">暂无未配置的本地模型</div>';
      return;
    }

    let html = '<div class="disc-models-scroll">';
    for (const fw of fwOrder) {
      const models = groups[fw] || [];
      if (models.length === 0) continue;
      const meta = fwMeta[fw] || { icon: '\uD83D\uDCE6', label: fw, canDeploy: false };
      for (const m of models) {
        const gb = m.size_mb >= 1024 ? (m.size_mb/1024).toFixed(1)+' GB' : m.size_mb+' MB';
        const isDeployed = activeNames.has(m.name);
        const statusCls = isDeployed ? 'deployed' : 'undeployed';
        const statusLabel = isDeployed ? '已部署' : '未部署';
        const tagCls = fw === 'vllm' ? 'vllm' : fw === 'ollama' ? 'ollama' : fw === 'ollama_cpp' ? 'ollama_cpp' : fw === 'comfyui' ? 'comfyui' : 'webui';

        html += '<div class="disc-model-card">';
        html += '<span class="disc-model-name" title="' + m.name + '">' + m.name + '</span>';
        html += '<span class="disc-model-tag ' + tagCls + '">' + meta.label + '</span>';
        if (m.size_mb) html += '<span class="disc-model-size">' + gb + '</span>';
        html += '<span class="disc-model-status ' + statusCls + '">' + statusLabel + '</span>';
        html += '<div class="disc-model-actions">';
        if (meta.canDeploy) {
          html += '<button class="disc-model-btn deploy" onclick="event.stopPropagation();doDeploy(\''+m.name+'\',\''+fw+'\')">Deploy</button>';
          html += '<button class="disc-model-btn pull" onclick="event.stopPropagation();doPullAndDeploy(\''+m.name+'\',\''+fw+'\')">Pull & Deploy</button>';
        }
        html += '</div></div>';
      }
    }
    html += '</div>';
    listEl.innerHTML = html;
  } catch(e) { /* ignore */ }
}

function toggleFw(hdr) {
  const body = hdr.nextElementSibling;
  const isOpen = hdr.classList.contains('open');
  hdr.classList.toggle('open', !isOpen);
  body.classList.toggle('open', !isOpen);
}

async function doDeploy(name, framework) {
  if (!swLock()) return;
  try {
    // Map framework to backend model_type
    const typeMap = { vllm: 'vllm', ollama: 'ollama', ollama_cpp: 'ollama_cpp' };
    const modelType = typeMap[framework] || framework;
    const r = await j('/deploy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name,type:modelType})});
    if (r.status === 'switched' || r.status === 'already_active') {
      toast(name+' 已部署 ✓', 'ok');
    } else {
      toast(r.message || '部署失败', 'err');
    }
  } catch(e) { toast(e.message, 'err'); }
  finally { sw=false; }
  await Promise.all([load(), loadModels(), loadLocalModels()]);
}

async function doPullAndDeploy(name, framework) {
  if (!swLock()) return;
  try {
    // Try /pull first; if not found, fall back to /deploy
    const typeMap = { vllm: 'vllm', ollama: 'ollama', ollama_cpp: 'ollama_cpp' };
    const modelType = typeMap[framework] || framework;
    let r;
    try {
      r = await j('/pull', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name, framework:framework})});
    } catch(e) {
      // /pull not available, fall back to /deploy (pull+deploy merged)
      r = await j('/deploy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name, type:modelType})});
    }
    if (r.status === 'switched' || r.status === 'already_active') {
      toast(name+' Pull & Deploy ✓', 'ok');
    } else {
      toast(r.message || '操作失败', 'err');
    }
  } catch(e) { toast(e.message, 'err'); }
  finally { sw=false; }
  await Promise.all([load(), loadModels(), loadLocalModels()]);
}

async function doReset() {
  if(!confirm('强制重置到 idle？'))return;
  const r=await j('/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  toast(r.status==='reset'?'已重置 ✓':'失败',r.status==='reset'?'ok':'err');
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

async function doReconcile() {
  const r=await j('/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const a=r.actions||[];
  toast(a.length===0?'状态一致 ✓':'修复: '+a.join('; '),'ok');
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

// ── Deploy Form Toggle ──
function toggleDeployForm() {
  const area = document.getElementById('deployFormArea');
  const btn = document.getElementById('deployFormToggle');
  const isOpen = area.style.display !== 'none';
  area.style.display = isOpen ? 'none' : '';
  btn.textContent = isOpen ? '展开部署表单' : '收起部署表单';
}

async function submitVllmDeploy(event) {
  event.preventDefault();
  if (!swLock()) return false;
  const form = event.target;
  const data = {
    name: form.name.value.trim(),
    type: 'vllm',
    model_dir: form.model_dir.value.trim(),
    gpu_memory_utilization: parseFloat(form.gpu_mem.value),
    max_context_length: parseInt(form.max_ctx.value, 10),
    quantization: form.quantization.value,
    inference_mode: form.inference_mode.value,
  };
  if (form.port.value) data.port = parseInt(form.port.value, 10);
  try {
    const r = await j('/deploy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    if (r.status === 'switched' || r.status === 'already_active') {
      toast('vLLM ['+data.name+'] 已部署 ✓', 'ok');
    } else {
      toast(r.message || '部署失败', 'err');
    }
  } catch(e) { toast(e.message, 'err'); }
  finally { sw=false; }
  await Promise.all([load(), loadModels(), loadLocalModels()]);
  return false;
}

async function submitOllamaDeploy(event) {
  event.preventDefault();
  if (!swLock()) return false;
  const form = event.target;
  const data = {
    name: form.name.value.trim(),
    type: 'ollama',
    ollama_ref: form.ollama_ref.value.trim(),
    gpu_layers: parseInt(form.gpu_layers.value, 10),
  };
  if (form.port.value) data.port = parseInt(form.port.value, 10);
  try {
    const r = await j('/deploy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    if (r.status === 'switched' || r.status === 'already_active') {
      toast('Ollama ['+data.name+'] 已部署 ✓', 'ok');
    } else {
      toast(r.message || '部署失败', 'err');
    }
  } catch(e) { toast(e.message, 'err'); }
  finally { sw=false; }
  await Promise.all([load(), loadModels(), loadLocalModels()]);
  return false;
}

// ── Usage Chart (P1) ──
let usageWindow='weekly';
async function loadUsage() {
  const stats = window.__TOKEN_STATS__ || {};
  const body = document.getElementById('usageBody');
  const tot = document.getElementById('usageTotal');
  
  if (!stats || !Object.keys(stats).length) {
    body.innerHTML = '<div class="usage-empty">暂无用量数据</div>';
    tot.textContent = '0 requests';
    return;
  }

  const now = new Date();
  const tz = 8; // UTC+8
  const windowMap = {
    daily: new Date(now - 24 * 3600 * 1000),
    weekly: new Date(now - 7 * 24 * 3600 * 1000),
    monthly: new Date(now - 30 * 24 * 3600 * 1000),
    all: null
  };
  const since = windowMap[usageWindow] || null;
  const sinceStr = since ? since.toISOString().split('T')[0] : null;

  // Aggregate by model
  const totals = {};
  for (const [date, models] of Object.entries(stats)) {
    if (sinceStr && date < sinceStr) continue;
    for (const [model, vals] of Object.entries(models)) {
      if (!totals[model]) totals[model] = { total_tokens: 0, requests: 0 };
      totals[model].total_tokens += (vals.prompt_tokens || 0) + (vals.generation_tokens || 0);
      totals[model].requests += (vals.requests || 0);
    }
  }

  const rows = Object.entries(totals).map(([m, d]) => ({ model: m, ...d }));
  if (!rows.length) {
    body.innerHTML = '<div class="usage-empty">暂无用量数据</div>';
    tot.textContent = '0 requests';
    return;
  }

  const maxTok = Math.max(...rows.map(r => r.total_tokens)) || 1;
  const totalReq = rows.reduce((s, r) => s + r.requests, 0);
  const totalTok = rows.reduce((s, r) => s + r.total_tokens, 0);
  tot.textContent = totalReq.toLocaleString() + ' reqs · ' + totalTok.toLocaleString() + ' tokens';

  const altCls = ['', 'alt', 'alt2', 'alt3'];
  body.innerHTML = rows.map((r, i) => {
    const pct = (r.total_tokens / maxTok * 100).toFixed(1);
    const cls = altCls[i % altCls.length];
    return '<div class="usage-bar-wrap">' +
      '<span class="usage-model-name" title="' + r.model + '">' + r.model + '</span>' +
      '<div class="usage-bar-track"><div class="usage-bar-f ' + cls + '" style="width:' + pct + '%"></div></div>' +
      '<span class="usage-tok-val">' + r.total_tokens.toLocaleString() + ' · ' + r.requests + ' reqs</span>' +
    '</div>';
  }).join('');
}
document.addEventListener('click',e=>{
  const t=e.target.closest('.usage-tab');
  if(!t)return;
  document.querySelectorAll('.usage-tab').forEach(b=>b.classList.remove('active'));
  t.classList.add('active');
  usageWindow=t.dataset.w;
  loadUsage();
});

// ── GPU Metrics (P1) ──
async function loadGpuMetrics() {
  try {
    const sys=await j('/system').catch(()=>({}));
    const g=document.getElementById('gpuGrid');
    const ts=document.getElementById('gpuTs');
    ts.textContent=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    const cards=[
      ['利用率', (sys.gpu_util_pct||0).toFixed(1), '%', sys.gpu_util_pct<30?'优秀':sys.gpu_util_pct<70?'中等':'高负载'],
      ['核心频率', sys.gpu_clock_mhz||'—', 'MHz', '实时'],
      ['功耗', (sys.gpu_power_w||0).toFixed(1), 'W', '实时'],
      ['显存负载', (((sys.gpu_used_mb||0)/(sys.gpu_total_mb||32607))*100||0).toFixed(1), '%', (sys.gpu_used_mb||0).toLocaleString()+' / '+(sys.gpu_total_mb||32607).toLocaleString()+' MB'],
    ];
    g.innerHTML=cards.map(c=>
      '<div class="gpu-card"><span class="gpu-card-label">'+c[0]+'</span>'+
      '<div><span class="gpu-card-val">'+c[1]+'</span><span class="gpu-card-unit">'+c[2]+'</span></div>'+
      '<span class="gpu-card-sub">'+c[3]+'</span></div>'
    ).join('');
  }catch(e){ /* ignore */ }
}

Promise.all([load(),loadModels(),loadLocalModels(),loadUsage(),loadGpuMetrics()]);
setInterval(()=>{load();loadModels();loadLocalModels();loadGpuMetrics();},5000);
setInterval(loadUsage,30000);
</script>
</body>
</html>"""