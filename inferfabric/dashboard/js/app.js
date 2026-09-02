// ── XSS-safe helpers ──
function esc(v) { if(v==null)return''; const d=document.createElement('div'); d.textContent=String(v); return d.innerHTML; }
function escAttr(v) { return String(v==null?'':v).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function swLock() {
  // Cross-tab lock via localStorage (shared across tabs)
  try {
    const key = 'inferfabric_sw_lock';
    const swT=Date.now();
    const stored = localStorage.getItem(key);
    if (stored && (swT - parseInt(stored, 10)) < 10000) return false;
    localStorage.setItem(key, String(swT));
    return true;
  } catch(e) { return true; } // localStorage unavailable → allow
}
function swUnlock() {
  try { localStorage.removeItem('inferfabric_sw_lock'); } catch(e) {}
}
window.addEventListener('beforeunload', swUnlock);

// toast → showToast (from bindings.js P0 migration)
const toast = window.showToast || function(m,t) { console.warn('[legacy-toast]',m,t); };

// ═══ Cloud Management (v4.7.0) ═══
let _cloudPresets = [];
let _selectedPreset = null;

function _esc(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

async function cloudLoadPresets() {
  try {
    const r = await fetch('/admin/cloud/presets', {headers: adminHeaders()});
    const d = await r.json();
    if (d.error) { document.getElementById('presetGrid').textContent = '加载失败'; return; }
    _cloudPresets = d.presets || [];
    const grid = document.getElementById('presetGrid');
    grid.innerHTML = '';
    if (!_cloudPresets.length) { grid.innerHTML = '<div class="svc-empty">无预设</div>'; return; }
    _cloudPresets.forEach(p => {
      const card = document.createElement('div');
      card.className = 'preset-card-mac';
      card.dataset.presetId = p.id;
      card.id = 'preset-' + p.id;
      card.innerHTML = '<span class="preset-icon">' + p.icon + '</span><span class="preset-name">' + esc(p.display_name) + '</span>';
      card.addEventListener('click', () => cloudSelectPreset(p.id));
      grid.appendChild(card);
    });
  } catch(e) { document.getElementById('presetGrid').textContent = '加载失败'; }
}

function cloudSelectPreset(id) {
  _selectedPreset = _cloudPresets.find(p => p.id === id);
  if (!_selectedPreset) return;
  // Highlight selected card
  document.querySelectorAll('.preset-card-mac').forEach(function(c){c.classList.remove('selected')});
  const card = document.getElementById('preset-' + id);
  if (card) { card.classList.add('selected'); }
  // Show preset form
  document.getElementById('presetForm').style.display = 'block';
  document.getElementById('presetSelected').textContent = _selectedPreset.icon + ' ' + _selectedPreset.display_name;
  document.getElementById('cpPresetApiKey').value = '';
  document.getElementById('cpPresetApiKey').focus();
  const envHint = _selectedPreset.env_var
    ? `Key 将存为 ${_selectedPreset.env_var}，写入 ~/.inferfabric/secrets.env`
    : `Key 将存为 IFF_${id.toUpperCase().replace(/-/g,'_')}_KEY`;
  document.getElementById('presetEnvHint').textContent = envHint;
}

function cloudDeselectPreset() {
  _selectedPreset = null;
  document.querySelectorAll('.preset-card-mac').forEach(function(c){c.classList.remove('selected')});
  document.getElementById('presetForm').style.display = 'none';
}

async function cloudAddPreset() {
  if (!_selectedPreset) { toast('请先选择预设', 'error'); return; }
  const apiKey = document.getElementById('cpPresetApiKey').value.trim();
  if (!apiKey) { toast('请填写 API Key', 'error'); return; }
  try {
    const r = await fetch('/admin/cloud/providers', {
      method: 'POST',
      headers: adminHeaders(),
      body: JSON.stringify({preset: _selectedPreset.id, api_key: apiKey})
    });
    const d = await r.json();
    if (d.error) { toast(d.error, 'error'); return; }
    toast(`Provider ${_selectedPreset.display_name} 已添加`, 'success');
    if (_selectedPreset.discovery) { await cloudDiscoverOne(_selectedPreset.id); }
    cloudDeselectPreset();
    cloudLoadProviders();
  } catch(e) { toast('添加失败: '+e, 'error'); }
}

async function cloudLoadProviders() {
  try {
    const r = await fetch('/admin/cloud/providers', {headers: adminHeaders()});
    const d = await r.json();
    if (d.error) { document.getElementById('cloudProvidersList').textContent = d.error; return; }
    const providers = d.providers || [];
    if (!providers.length) {
      document.getElementById('cloudProvidersList').innerHTML = '<div class="svc-empty">暂无 Provider — 使用上方表单添加</div>';
      document.getElementById('cloudModelsList').innerHTML = '<div class="svc-empty">添加 Provider 并发现模型后显示</div>';
      document.getElementById('cloudModelCount').textContent = '0 个模型';
      return;
    }
    let html = '';
    providers.forEach(p => {
      const st = p.enabled ? '✓' : '✗';
      const stColor = p.enabled ? '#4ade80' : '#f87171';
      const oi = p.openai_base ? '✓' : '-';
      const ai = p.anthropic_base ? '✓' : '-';
      // v4.7.0: Key ENV status
      const keyStatus = p.key_env_set ? '✅ 已设置' : '⚠️ ENV 未设置';
      const keyLine = p.key_env_var ? `<span style="font-size:11px;color:var(--text3)">Key: \${${p.key_env_var}} ${keyStatus}</span>` : '';
      // Preset icon
      const presetIcon = p.preset_id ? (_cloudPresets.find(pr => pr.id === p.preset_id) || {}).icon || '' : '';
      html += `<div class="disc-card">
        <div class="disc-info">
          <div class="disc-name">${presetIcon ? presetIcon + ' ' : ''}${p.name}</div>
          <div class="disc-meta">
            <span style="color:${stColor}">${st}</span>
            · OpenAI ${oi} · Anthropic ${ai}
            · ${p.model_count} 模型
            · ${p.discovery_interval}s 间隔
          </div>
          ${keyLine ? '<div style="margin-top:2px">' + keyLine + '</div>' : ''}
        </div>
        <div style="display:flex;gap:4px;flex-shrink:0">
          <button class="disc-model-btn deploy" onclick="cloudDiscoverOne('${p.name}')">🔍 发现</button>
          <button class="disc-model-btn pull" onclick="cloudDeleteProvider('${p.name}')">🗑 删除</button>
        </div>
      </div>`;
    });
    document.getElementById('cloudProvidersList').innerHTML = html;
    cloudLoadModels(d);
  } catch(e) { document.getElementById('cloudProvidersList').textContent = '加载失败: '+e; }
}

var _cloudModelIds = [];
window._cloudModelIds = _cloudModelIds;

function cloudLoadModels(data) {
  const models = (data && data.models) || [];
  document.getElementById('cloudModelCount').textContent = models.length + ' 个模型';
  if (!models.length) {
    document.getElementById('cloudModelsList').innerHTML = '<div class="svc-empty">暂无模型 — 点击 Provider 的 🔍 发现</div>';
    return;
  }
  let html = '';
  models.sort((a,b) => (b.discovered_at||0) - (a.discovered_at||0));
  _cloudModelIds = models.map(function(m) { return { id: m.id, provider: m.provider }; });
  models.forEach(m => {
    const proto = [];
    if (m.openai_available) proto.push('<span class="disc-model-tag" style="background:var(--green-s);color:var(--green)">OpenAI</span>');
    if (m.anthropic_available) proto.push('<span class="disc-model-tag" style="background:var(--purple-s);color:var(--purple)">Anthropic</span>');
    const caps = m.capabilities || {};
    let capHtml = '';
    if (caps.context_window) capHtml += `<span class="spec-tag">ctx ${caps.context_window >= 1024 ? (caps.context_window/1024)+'K' : caps.context_window}</span>`;
    if (caps.max_output_tokens) capHtml += `<span class="spec-tag">out ${caps.max_output_tokens >= 1024 ? (caps.max_output_tokens/1024)+'K' : caps.max_output_tokens}</span>`;
    if (caps.supports_vision) capHtml += '<span class="spec-tag" style="color:var(--purple)">👁</span>';
    if (caps.supports_tools) capHtml += '<span class="spec-tag" style="color:var(--blue)">🔧</span>';
    Object.keys(caps).forEach(k => {
      if (!["context_window","max_output_tokens","supports_vision","supports_tools"].includes(k)) {
        capHtml += `<span class="spec-tag">${k}: ${caps[k]}</span>`;
      }
    });
    const source = m.discovered_at > 0 ? '<span class="disc-model-status deployed">已发现</span>' : '<span class="disc-model-status undeployed">仅配置</span>';
    html += `<div class="disc-model-card" style="height:auto;min-height:44px;padding:8px 10px;flex-wrap:wrap">
      <span class="disc-model-name">${m.id}</span>
      ${proto.join('')}
      <span style="font-size:11px;color:var(--text3)">${m.provider}</span>
      ${capHtml}
      ${source}
    </div>`;
  });
  document.getElementById('cloudModelsList').innerHTML = html;
}

async function cloudAddProvider() {
  const name = document.getElementById('cpName').value.trim();
  const apiKey = document.getElementById('cpApiKey').value.trim();
  const openaiBase = document.getElementById('cpOpenaiBase').value.trim();
  const anthropicBase = document.getElementById('cpAnthropicBase').value.trim();
  if (!name) { toast('请填写 Provider 名称', 'error'); return; }
  if (!openaiBase && !anthropicBase) { toast('至少填写一个 Base URL', 'error'); return; }
  try {
    const r = await fetch('/admin/cloud/providers', {
      method: 'POST',
      headers: adminHeaders(),
      body: JSON.stringify({name, api_key: apiKey, openai_base: openaiBase, anthropic_base: anthropicBase})
    });
    const d = await r.json();
    if (d.error) { toast(d.error, 'error'); return; }
    toast(`Provider ${name} 已添加`, 'success');
    await cloudDiscoverOne(name);
    document.getElementById('cpName').value = '';
    document.getElementById('cpApiKey').value = '';
    document.getElementById('cpOpenaiBase').value = '';
    document.getElementById('cpAnthropicBase').value = '';
  } catch(e) { toast('添加失败: '+e, 'error'); }
}

async function cloudTestProvider() {
  const openaiBase = document.getElementById('cpOpenaiBase').value.trim();
  const apiKey = document.getElementById('cpApiKey').value.trim();
  if (!openaiBase) { toast('请填写 OpenAI Base URL', 'error'); return; }
  toast('正在测试连接...', 'info');
  try {
    const r = await fetch('/admin/cloud/test', {
      method: 'POST',
      headers: adminHeaders(),
      body: JSON.stringify({url: openaiBase.replace(/\/+$/, '') + '/models', api_key: apiKey})
    });
    const d = await r.json();
    if (d.error) { toast('连接失败: ' + d.error, 'error'); return; }
    toast(`连接成功! 发现 ${d.model_count || 0} 个模型`, 'success');
  } catch(e) { toast('测试失败: '+e, 'error'); }
}

async function cloudDiscoverAll() {
  try {
    const r = await fetch('/admin/cloud/discover', {method:'POST', headers: adminHeaders()});
    const d = await r.json();
    if (d.error) { toast(d.error, 'error'); return; }
    toast(`发现 ${d.cloud_models} 个云端模型`, 'success');
    cloudLoadProviders();
  } catch(e) { toast('发现失败: '+e, 'error'); }
}

async function cloudDiscoverOne(providerName) {
  try {
    const r = await fetch('/admin/cloud/discover', {
      method: 'POST',
      headers: adminHeaders(),
      body: JSON.stringify({provider: providerName})
    });
    const d = await r.json();
    if (d.error) { toast(d.error, 'error'); return; }
    toast(`Provider ${providerName}: 发现 ${d.cloud_models} 个模型`, 'success');
    cloudLoadProviders();
  } catch(e) { toast('发现失败: '+e, 'error'); }
}

async function cloudDeleteProvider(providerName) {
  if (!confirm(`确定删除 Provider "${providerName}"？`)) return;
  try {
    const r = await fetch('/admin/cloud/providers', {
      method: 'DELETE',
      headers: adminHeaders(),
      body: JSON.stringify({name: providerName})
    });
    const d = await r.json();
    if (d.error) { toast(d.error, 'error'); return; }
    toast(`Provider ${providerName} 已删除`, 'success');
    cloudLoadProviders();
  } catch(e) { toast('删除失败: '+e, 'error'); }
}

async function cloudReload() {
  try {
    const r = await fetch('/admin/cloud/reload', {method:'POST', headers: adminHeaders()});
    const d = await r.json();
    if (d.error) { toast(d.error, 'error'); return; }
    toast(`已重新加载: ${d.providers} 个 provider, ${d.cloud_models} 个模型`, 'success');
    cloudLoadProviders();
  } catch(e) { toast('加载失败: '+e, 'error'); }
}

function adminHeaders() {
  const h = {'Content-Type':'application/json'};
  const tk = document.getElementById('adminToken')?.value || new URLSearchParams(location.search).get('token') || '';
  if (tk) h['X-Admin-Token'] = tk;
  return h;
}

// Cloud data load on tab switch — delegated to state.js switchTab
// Cloud tab handler triggered by state store subscription
if (window.store) {
  window.store.on('tab_active', (val) => {
    if (val === 'tab-cloud') { cloudLoadPresets(); cloudLoadProviders(); }
  });
}
async function j(p,o) { return (await fetch(p,o)).json(); }

async function load() {
  // v5.x: render from the /api/snapshot store (single source of truth).
  let s = store.get('status');
  if (!s) {
    const snap = await store.forceRefresh();
    s = (snap && snap.status) || {};
  }
  let hist = store.get('history');
  if (!hist) {
    hist = await j('/history').catch(() => []);
  }

  // History table (switch log) — unique to this view, not in status API
  const hBody=document.getElementById('hBody');
  if(!hist||!hist.length){hBody.innerHTML='<div style="text-align:center;padding:20px;color:var(--text4);font-size:14px">暂无记录</div>';}
  else{hBody.innerHTML=hist.slice(0,12).map(h=>{
    const t=h.timestamp?new Date(h.timestamp+'Z'):new Date();
    const ts=t.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    const d=h.duration!=null?h.duration.toFixed(1)+'s':'—';
    const st=h.status==='ok'?'<span class="h-ok">✓</span>':'<span class="h-err">✗</span>';
    return '<div class="hrow"><span class="h-time">'+ts+'</span><span class="h-from">'+esc(h.from||'—')+'</span><span class="h-arrow">→</span><span class="h-to">'+esc(h.to)+'</span><span class="h-dur">'+d+'</span><span>'+st+'</span></div>';
  }).join('');}

  // vLLM metrics detection
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
    clearInterval(vllmTimer);
    vllmTimer=null;
  }

  // Active services row layout
  const svcs=s.active_services||[];
  const health=s.services_health||{};
  const sInfo=s.services_info||{};
  const svcCard=document.getElementById('svcCard');
  let svcHtml='<div class="if-card-hdr" style="margin-bottom:'+(svcs.length?12:0)+'px"><div class="if-card-icon" style="background:var(--blue-s);color:var(--blue)"><svg width="15" height="15"><use href="#s-cube"/></svg></div><span class="if-card-title">活跃服务</span></div>';
  if(svcs.length===0){
    svcHtml+='<span class="svc-empty">无活跃服务</span>';
  }else{
    svcHtml+='<div style="display:flex;gap:8px;flex-wrap:wrap">';
    for(const n of svcs){
      const h=health[n]||"❌";
      const ok=h==='✅';
      const info=sInfo[n]||{};
      const port=info.port||'—';
      const mode=info.mode||'?';
      svcHtml+='<div style="flex:1;min-width:180px;display:flex;align-items:center;gap:6px;padding:6px 10px;background:var(--bg);border:1px solid var(--border);border-radius:8px">';
      svcHtml+='<span style="color:'+(ok?'var(--green)':'var(--red)')+';font-size:14px">'+(ok?'✓':'✗')+'</span>';
      svcHtml+='<span style="flex:1;font-size:13px;font-weight:600;color:var(--text1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+n+'</span>';
      svcHtml+='<span style="font-size:11px;color:var(--text3);font-variant-numeric:tabular-nums;flex-shrink:0">:'+port+'</span>';
      svcHtml+='</div>';
    }
    svcHtml+='</div>';
  }
  svcCard.innerHTML=svcHtml;
}

// ── Overview (P2→P6 验收：激活模型详细状态卡片) ──

function ovFormatUptime(sec) {
  if (sec == null || sec === undefined) return '—';
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  let out = '';
  if (d) out += d + 'd ';
  if (h) out += h + 'h ';
  out += m + 'm ' + s + 's';
  return out;
}
async function loadOverview() {
  let models = store.get('models');
  let st = store.get('status');
  if (!models || !st) {
    const snap = await store.forceRefresh();
    models = (snap && snap.models) || [];
    st = (snap && snap.status) || {};
  }
  const svcs = st.active_services || [];
  const health = st.services_health || {};
  const sInfo = st.services_info || {};
  const sleepSt = st.sleep_states || {};

  const countEl = document.getElementById('ovActiveCount');
  if (countEl) countEl.textContent = String(svcs.length);
  const activeEl = document.getElementById('ovActive');
  if (activeEl) {
    if (svcs.length === 0) {
      activeEl.innerHTML = '<div class="if-empty">暂无激活模型</div>';
    } else {
      // 卡片式：与"推理"tab 的 model-card 同一视觉语言
      const FW_ICONS = { vllm:'🔥', ollama:'🦙', sglang:'📦', comfyui:'🎨', asr_server:'🎤', tts_server:'🔊', ollama_cpp:'📦' };
      const FW_LABELS = { vllm:'vLLM', ollama:'Ollama', sglang:'SGLang', comfyui:'ComfyUI', asr_server:'ASR', tts_server:'TTS', ollama_cpp:'ollama.cpp' };
      const MT_ICON = { llm:'🧠', vl:'👁', omni:'🌐', ocr:'📄', aigc:'✨', embedding:'📊', rerank:'🔄', infra:'⚙️', tts:'🔊', asr:'🎤' };
      let html = '<div class="model-grid">';
      for (const name of svcs) {
        const info = sInfo[name] || {};
        const m = models.find(x => x.name === name) || {};
        const ec = m.engine_config || {};
        const ok = (health[name] || '❌') === '✅';
        const sleeping = !!sleepSt[name];
        const badgeCls = info.mode === 'shared' ? 'shrd' : 'excl';
        const badgeLabel = info.mode === 'shared' ? '共享' : '独占';
        const statusCls = sleeping ? 'sleeping' : (ok ? 'running' : 'stopped');
        const statusIcon = sleeping ? '⏸' : (ok ? '✅' : '✗');
        const statusText = sleeping ? '休眠中' : (ok ? '运行中' : '异常');
        const engineTags = [];
        // vLLM params
        if (ec.max_num_seqs != null) engineTags.push('Batch: ' + ec.max_num_seqs);
        if (ec.gpu_memory_utilization != null) engineTags.push('VRAM: ' + (ec.gpu_memory_utilization * 100).toFixed(0) + '%');
        if (ec.kv_cache_dtype) engineTags.push('KV: ' + ec.kv_cache_dtype);
        if (ec.max_batched_tokens != null) engineTags.push('Chunk: ' + ec.max_batched_tokens);
        if (ec.chunked_prefill != null) engineTags.push('Prefill: ' + (ec.chunked_prefill ? '✓' : '✗'));
        if (ec.kv_offloading_size != null) engineTags.push('Offload: ' + Number(ec.kv_offloading_size).toFixed(0) + 'GB');
        // SGLang params
        if (ec.max_running_requests != null) engineTags.push('MaxReq: ' + ec.max_running_requests);
        if (ec.context_length != null) engineTags.push('Ctx: ' + ec.context_length);
        if (ec.mem_fraction != null) engineTags.push('VRAM: ' + (ec.mem_fraction * 100).toFixed(0) + '%');
        if (ec.enable_lmcache != null) engineTags.push('LMCache: ' + (ec.enable_lmcache ? '✓' : '✗'));
        // ollama_cpp params
        if (ec.threads != null) engineTags.push('Threads: ' + ec.threads);
        if (ec.context_size != null) engineTags.push('Ctx: ' + ec.context_size);
        // ollama_cpp params
        if (ec.conda_env && engineTags.length === 0) engineTags.push('Env: ' + ec.conda_env);
        const engineHtml = engineTags.map(t => '<span class="card-tag">' + t + '</span>').join('');
        // Layer 3: original model name (from description, first part)
        const origName = m.description ? m.description.replace(/[—–].*$/, '').trim() : m.name;
        html += '<div class="model-card active">' +
          '<div class="card-hdr">' +
            '<div class="card-icon-box">' + (FW_ICONS[info.type] || '\u{1f4e6}') + '</div>' +
            '<div class="card-name">' + name + '</div>' +
            '<span class="card-badge ' + badgeCls + '">' + badgeLabel + '</span>' +
          '</div>' +
          '<div class="card-status ' + statusCls + '">' +
            '<span class="st-icon">' + statusIcon + '</span>' +
            '<span class="st-text">' + statusText + '</span>' +
            '<span class="st-port">:' + (info.port != null ? info.port : '—') + '</span>' +
          '</div>' +
          '<div class="card-tags">' + engineHtml + '</div>' +
          '<div class="ov-desc" style="font-size:12px;color:var(--if-text-3)">' + origName + '</div>' +
        '</div>';
      }
      html += '</div>';
      activeEl.innerHTML = html;
    }
  }

  // GPU & 系统状态
  const gpu = store.get('gpu') || {};
  const gpuUtil = store.get('gpu_util') || {};
  const cpu = store.get('cpu') || {};
  const mem = store.get('mem') || {};
  const ver = store.get('version') || '';
  const pct = gpu.pct != null ? Math.min(100, Math.max(0, gpu.pct)) : 0;
  const barCls = pct > 90 ? 'crit' : pct > 70 ? 'warn' : '';
  const tile = (val, label) =>
    '<div class="m-stat">' +
    '<span class="m-stat-val" style="font-size:16px">' + val + '</span>' +
    '<span class="m-stat-label">' + label + '</span></div>';
  const gpuMemTile =
    '<div class="m-stat">' +
    '<span class="m-stat-val" style="font-size:16px">' + pct.toFixed(1) + '%</span>' +
    '<span class="m-stat-label">显存 ' + Number(gpu.used || 0).toLocaleString() + ' / ' + Number(gpu.total || 0).toLocaleString() + ' MB</span>' +
    '<div style="height:5px;background:var(--if-surface-2);border-radius:999px;overflow:hidden;margin-top:8px"><div class="perf-bar-f ' + barCls + '" style="height:100%;width:' + pct + '%"></div></div>' +
    '</div>';
  const gpuEl = document.getElementById('ovGpu');
  if (gpuEl) {
    gpuEl.innerHTML =
      '<div class="m-overview-grid">' +
      gpuMemTile +
      tile(gpuUtil.pct != null ? Number(gpuUtil.pct).toFixed(1) + '%' : '—', 'GPU 利用率') +
      tile((gpuUtil.clock != null && gpuUtil.clock !== '—') ? gpuUtil.clock : '—', 'GPU 频率 (MHz)') +
      tile((gpuUtil.power != null && gpuUtil.power !== '—') ? gpuUtil.power : '—', 'GPU 功耗 (W)') +
      '</div>';
  }
  const sysEl = document.getElementById('ovSys');
  if (sysEl) {
    sysEl.innerHTML =
      '<div class="m-overview-grid">' +
      tile(cpu.pct != null ? Number(cpu.pct).toFixed(1) + '%' : '—', 'CPU 使用率') +
      tile((cpu.cores != null && cpu.cores !== '—') ? String(cpu.cores) : '—', 'CPU 核心数') +
      tile((mem.used || 0) + ' GB', '内存 / ' + (mem.total || 0) + ' GB') +
      tile(ovFormatUptime(cpu.uptime), '运行时长') +
      '</div>';
  }
}

// ── vLLM 实时性能（总览，60s 轮询，仅总览 tab 激活时刷新）──
let ovPerfTimer = null;
async function ovPerfTick() {
  const st = store.get('status') || {};
  const sInfo = st.services_info || {};
  const svcs = st.active_services || [];
  const perfEl = document.getElementById('ovPerf');
  const portEl = document.getElementById('ovPerfPort');
  if (!perfEl) return;
  const vllmSvc = svcs.find(n => sInfo[n] && sInfo[n].type === 'vllm');
  if (!vllmSvc) {
    if (portEl) portEl.textContent = '—';
    perfEl.innerHTML = '<div class="if-empty">无活跃 vLLM 服务</div>';
    return;
  }
  const port = sInfo[vllmSvc] ? sInfo[vllmSvc].port : null;
  if (portEl) portEl.textContent = 'port ' + port;
  try {
    const m = await j('/vllm_metrics?port=' + port);
    if (m.error) {
      perfEl.innerHTML = '<div class="if-empty">指标暂不可用（' + m.error + '）</div>';
      return;
    }
    const perfCard = (label, tip, main, sub, bar) => {
      let s = '<div class="perf-card">';
      s += '<span class="perf-label">' + label + '</span>';
      if (tip) s += '<span class="perf-tip">' + tip + '</span>';
      if (bar != null) {
        const bcls = bar > 90 ? 'crit' : bar > 70 ? 'warn' : '';
        s += '<span class="perf-main">' + main + '</span>';
        s += '<div class="perf-bar"><div class="perf-bar-f ' + bcls + '" style="width:' + Math.min(100, bar).toFixed(1) + '%"></div></div>';
      } else {
        s += '<span class="perf-main">' + main + '</span>';
        if (sub) s += '<span class="perf-sub">' + sub + '</span>';
      }
      s += '</div>';
      return s;
    };
    const tpot = m.tpot_seconds || {};
    const tf = m.ttft_seconds || {};
    let html = '<div class="perf-cards">';
    const kv = m.kv_cache_usage_perc ?? 0;
    html += perfCard('KV Cache', 'GPU KV 缓存占用率。>90% 容易触发抢占，导致延迟飙升', kv.toFixed(1) + '%', null, kv);
    if (m.seq_length != null) {
      html += perfCard('Seq Length', '平均请求长度（Prompt + Generation 总 Token 数）',
        Number(m.seq_length).toLocaleString() + ' tokens',
        'P ' + (m.seq_prompt != null ? Number(m.seq_prompt).toLocaleString() : '—') + ' + G ' + (m.seq_generation != null ? Number(m.seq_generation).toLocaleString() : '—') + ' (' + (m.seq_count || 0) + ' requests)');
    }
    if (m.tpot_cum_mean != null) {
      html += perfCard('TPOT', 'Time Per Output Token（秒）',
        (m.tpot_cum_mean * 1000).toFixed(1) + ' ms',
        'P50 ' + ((tpot.p50 || 0) * 1000).toFixed(1) + 'ms | P95 ' + ((tpot.p95 || 0) * 1000).toFixed(1) + 'ms | ' + (m.tpot_cum_n || 0) + ' reqs');
    }
    if (m.ttft_cum_mean != null) {
      html += perfCard('TTFT', 'Time to First Token（秒）',
        m.ttft_cum_mean.toFixed(2) + 's',
        '累计 ' + (m.ttft_cum_n || 0) + ' 次 | P50 ' + (tf.p50 != null ? tf.p50.toFixed(2) : '—') + 's | P95 ' + (tf.p95 != null ? tf.p95.toFixed(2) : '—') + 's');
    } else if (tf.mean != null) {
      html += perfCard('TTFT', 'Time to First Token（秒）',
        tf.mean.toFixed(2) + 's',
        'P50 ' + (tf.p50 != null ? tf.p50.toFixed(2) : '—') + 's | P95 ' + (tf.p95 != null ? tf.p95.toFixed(2) : '—') + 's | ' + (tf.count || 0) + ' 次');
    }
    if (m.throughput != null) {
      let subText = 'total ' + (m.throughput_cum_n != null ? Number(m.throughput_cum_n).toLocaleString() : '0') + ' tokens';
      if (m.throughput_inst != null && m.throughput_inst !== undefined)
        subText += ' | ' + m.throughput_inst + ' t/s (10s)';
      html += perfCard('Throughput', '生成吞吐量（tokens/s）', m.throughput + ' t/s (EMA)', subText);
    }
    html += '</div>';
    perfEl.innerHTML = html;
  } catch (e) {
    perfEl.innerHTML = '<div class="if-empty">指标获取失败: ' + e.message + '</div>';
  }
}
function ovPerfStart() {
  if (ovPerfTimer) return;
  ovPerfTimer = setInterval(() => {
    const tab = document.getElementById('tab-overview');
    if (tab && tab.classList.contains('active')) ovPerfTick();
  }, 60000);
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
  // v5.x: use the snapshot store; force a fresh snapshot if absent.
  let models = store.get('models');
  let st = store.get('status');
  if (!models || !st) {
    const snap = await store.forceRefresh();
    models = (snap && snap.models) || [];
    st = (snap && snap.status) || {};
  }
  const excl=models.filter(m=>m.mode==='exclusive');
  const shrd=models.filter(m=>m.mode==='shared');
  const free=models.filter(m=>m.mode==='none');
  const sleepSt=st.sleep_states||{};
  const svcInfo=st.services_info||{};

  function renderCard(m, modeBadge) {
    const info = svcInfo[m.name] || {};
    const port = info.port || '—';
    const sleeping = !!sleepSt[m.name];
    const active = m.active && !sleeping;
    const isVllm = m.type === 'vllm';

    const fwIcons = { vllm: '🔥', ollama: '🦙', sglang: '📦', comfyui: '🎨', asr_server: '🎤', tts_server: '🔊', ollama_cpp: '📦' };
    const fwLabels = { vllm: 'vLLM', ollama: 'Ollama', sglang: 'SGLang', comfyui: 'ComfyUI', asr_server: 'ASR', tts_server: 'TTS', ollama_cpp: 'ollama.cpp' };
    const mtIcon = { llm: '🧠', vl: '👁', omni: '🌐', ocr: '📄', aigc: '✨', embedding: '📊', rerank: '🔄', infra: '⚙️', tts: '🔊', asr: '🎤' };

    const cls = 'model-card' + (active ? ' active' : '');
    const statusCls = active ? 'running' : (sleeping ? 'sleeping' : 'stopped');
    const statusIcon = active ? '✅' : (sleeping ? '⏸' : '○');
    const statusText = active ? 'running' : (sleeping ? 'sleeping' : 'stopped');

    const tags = [];
    if (m.quantization) tags.push(m.quantization);
    if (m.context_window && m.context_window >= 1024) tags.push((m.context_window / 1024).toFixed(0) + 'K ctx');
    else if (m.context_window) tags.push(m.context_window + ' ctx');
    tags.push(fwLabels[m.type] || m.type);
    if (m.model_type) tags.push((mtIcon[m.model_type] || '🧠') + ' ' + m.model_type);
    const tagsHtml = tags.map(function(t) { return '<span class="card-tag">' + t + '</span>'; }).join('');

    var ctaBtn = '';
    if (active) {
        ctaBtn = '<button class="card-cta stop" onclick="event.stopPropagation();doRelease(\'' + escAttr(m.name) + '\',' + (m.mode === 'exclusive') + ')">释放</button>';
        if (isVllm) ctaBtn += '<button class="card-cta sleep" onclick="event.stopPropagation();doSleep(\'' + escAttr(m.name) + '\')" style="margin-top:4px">休眠</button>';
    } else if (sleeping) {
        ctaBtn = '<button class="card-cta stop" onclick="event.stopPropagation();doRelease(\'' + escAttr(m.name) + '\',' + (m.mode === 'exclusive') + ')">释放</button>';
        if (isVllm) ctaBtn += '<button class="card-cta start" onclick="event.stopPropagation();doWake(\'' + escAttr(m.name) + '\')" style="margin-top:4px">唤醒</button>';
    } else {
        ctaBtn = '<button class="card-cta start" onclick="event.stopPropagation();doSwitch(\'' + escAttr(m.name) + '\')">启动</button>';
    }

    var badgeLabel = modeBadge === 'excl' ? '独占' : (modeBadge === 'shrd' ? '共享' : '空闲');

    return '<div class="' + cls + '">' +
        '<div class="card-hdr">' +
        '<div class="card-icon-box">' + (fwIcons[m.type] || '📦') + '</div>' +
        '<div class="card-name">' + esc(m.name) + '</div>' +
        '<span class="card-badge ' + modeBadge + '">' + badgeLabel + '</span>' +
        '</div>' +
        '<div class="card-status ' + statusCls + '">' +
        '<span class="st-icon">' + statusIcon + '</span>' +
        '<span class="st-text">' + statusText + '</span>' +
        '<span class="st-port">:' + port + '</span>' +
        '</div>' +
        '<div class="card-tags">' + tagsHtml + '</div>' +
        ctaBtn +
        '</div>';
}

  const setCt = function(id, n) { var el = document.getElementById(id); if (el) el.textContent = String(n); };
  setCt('exclCount', excl.length);
  setCt('shrdCount', shrd.length);
  setCt('freeCount', free.length);
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
  finally{swUnlock();}
  await store.forceRefresh();
}

async function doSleep(n) {
  if(!swLock())return;
  try{
    const r=await j('/sleep',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='ok') toast(n+' 休眠 ✓','ok');
    else if(r.status==='already_sleeping') toast(n+' 已在休眠','info');
    else toast(r.message||'失败','err');
  }catch(e){toast(e.message,'err');}
  finally{swUnlock();}
  await store.forceRefresh();
}

async function doWake(n) {
  if(!swLock())return;
  try{
    const r=await j('/wake',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='ok') toast(n+' 已唤醒 ✓','ok');
    else if(r.status==='already_awake') toast(n+' 未休眠','info');
    else toast(r.message||'失败','err');
  }catch(e){toast(e.message,'err');}
  finally{swUnlock();}
  await store.forceRefresh();
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
  finally{swUnlock();}
  await store.forceRefresh();
}

async function doStop(n) {
  if(!swLock())return;
  try{
    const r=await j('/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='stopped') toast(n+' 已停止','ok');
    else if(r.status==='already_stopped') toast(n+' 未运行','info');
    else toast(r.message||'停止失败','err');
  }catch(e){toast(e.message,'err');}
  finally{swUnlock();}
  await store.forceRefresh();
}

async function loadLocalModels() {
  try {
    // v5.x: local-models + status from the snapshot store
    let d = store.get('local_models');
    let st = store.get('status');
    if (!d || !st) {
      const snap = await store.forceRefresh();
      d = (snap && snap.local_models) || { discovered: [], configured: [] };
      st = (snap && snap.status) || {};
    }
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

    // Use fw-group/fw-hdr/fw-body collapsible structure (CSS already defined)
    let html = '';
    for (const fw of fwOrder) {
      const models = groups[fw] || [];
      if (models.length === 0) continue;
      const meta = fwMeta[fw] || { icon: '\uD83D\uDCE6', label: fw, canDeploy: false };
      const fwIconCls = fw === 'vllm' ? 'vllm' : fw === 'ollama' ? 'ollama' : fw === 'ollama_cpp' ? 'ollama_cpp' : fw === 'comfyui' ? 'comfyui' : 'webui';
      const deployTag = meta.canDeploy ? '<span class="fw-deploy-tag yes">可部署</span>' : '<span class="fw-deploy-tag no">仅发现</span>';

      html += '<div class="fw-group">';
      html += '<div class="fw-hdr open" onclick="toggleFw(this)">';
      html += '<span class="fw-chevron">\u25B6</span>';
      html += '<span class="fw-icon ' + fwIconCls + '">' + meta.icon + '</span>';
      html += '<span class="fw-label">' + meta.label + '</span>';
      html += '<span class="fw-count">(' + models.length + ')</span>';
      html += deployTag;
      html += '</div>';
      html += '<div class="fw-body open">';
      for (const m of models) {
        const gb = m.size_mb >= 1024 ? (m.size_mb/1024).toFixed(1)+' GB' : m.size_mb+' MB';
        const isDeployed = activeNames.has(m.name);
        const statusCls = isDeployed ? 'deployed' : 'undeployed';
        const statusLabel = isDeployed ? '已部署' : '未部署';

        // Simplified card: name + size + Deploy button only
        html += '<div class="disc-card">';
        html += '<div class="disc-info">';
        html += '<div class="disc-name">' + esc(m.name) + '</div>';
        html += '<div class="disc-meta">' + gb + ' · <span class="disc-model-status ' + statusCls + '">' + statusLabel + '</span></div>';
        html += '</div>';
        if (meta.canDeploy) {
          html += '<button class="disc-deploy" onclick="event.stopPropagation();doDeploy(\''+escAttr(m.name)+'\',\''+fw+'\')">Deploy</button>';
        }
        html += '</div>';
      }
      html += '</div>';
      html += '</div>';
    }

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
  finally { swUnlock(); }
  await store.forceRefresh();
}

async function doPullAndDeploy(name, framework) {
  if (!swLock()) return;
  try {
    const typeMap = { vllm: 'vllm', ollama: 'ollama', ollama_cpp: 'ollama_cpp' };
    const modelType = typeMap[framework] || framework;
    // Phase 1: Pull
    const pullResult = await j('/pull', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name, framework:framework})});
    if (pullResult.status === 'error') {
      toast(pullResult.message || 'Pull 失败', 'err');
      return;
    }
    // Phase 2: Deploy (only after successful pull)
    const deployResult = await j('/deploy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name, type:modelType})});
    if (deployResult.status === 'switched' || deployResult.status === 'already_active') {
      toast(name+' Pull & Deploy ✓', 'ok');
    } else {
      toast(deployResult.message || '部署失败', 'err');
    }
  } catch(e) { toast(e.message, 'err'); }
  finally { swUnlock(); }
  await store.forceRefresh();
}

async function doReset() {
  if(!confirm('强制重置到 idle？'))return;
  const r=await j('/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  toast(r.status==='reset'?'已重置 ✓':'失败',r.status==='reset'?'ok':'err');
  await store.forceRefresh();
}

async function doReconcile() {
  const r=await j('/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const a=r.actions||[];
  toast(a.length===0?'状态一致 ✓':'修复: '+a.join('; '),'ok');
  await store.forceRefresh();
}

async function doReloadConfig() {
  const r=await j('/reload-config',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  if(r.status==='reloaded') {
    toast('配置已重载 ✓','ok');
    await store.forceRefresh();
  } else {
    toast(r.message||'重载失败','err');
  }
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
  finally { swUnlock(); }
  await store.forceRefresh();
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
  finally { swUnlock(); }
  await store.forceRefresh();
  return false;
}

// ── Usage Chart (P1) ──
let usageWindow='weekly';
async function loadUsage() {
  // v5.x: token stats come from the /api/snapshot store (no extra fetch)
  let stats = store.get('token_stats');
  if (!stats) {
    const snap = await store.forceRefresh();
    stats = (snap && snap.token_stats) || window.__TOKEN_STATS__ || {};
  }
  const body = document.getElementById('usageBody');
  const tot = document.getElementById('usageTotal');
  if (!body) return;  // G-3a: usage panel moved to monitor.js
  
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

// ── 本地模型使用曲线 (v5.7) ──
let ovTokenGranularity = 'hour';

async function loadTokenCurve() {
  const el = document.getElementById('ovTokenCurveChart');
  if (!el) return;
  try {
    const cw = el.clientWidth > 0 ? el.clientWidth : 760; // viewBox 宽度=容器宽度 → 缩放比 1:1
    const r = await j('/api/token-curve?granularity=' + ovTokenGranularity);
    const buckets = r.local || [];
    const hasData = buckets.some(b => (b.tokens || 0) > 0);
    if (!hasData) {
      el.innerHTML = '<div class="if-empty">暂无使用数据</div>';
      return;
    }
    el.innerHTML = renderTokenCurve(r, cw);
  } catch (e) {
    el.innerHTML = '<div class="if-empty">' + esc(e.message || '加载失败') + '</div>';
  }
}

function fmtTok(v) {
  if (v >= 1e9) return Math.round(v / 1e9) + 'B';
  if (v >= 1e6) return Math.round(v / 1e6) + 'M';
  if (v >= 1e3) return Math.round(v / 1e3) + 'K';
  return String(v);
}

function renderTokenCurve(r, W) {
  const g = r.granularity;
  const buckets = r.local || [];
  W = W || 760; // viewBox 宽度跟随容器，保证 1:1 渲染
  const H = 156; // 框整体放大 20%
  const pad = { t: 8, r: 12, b: 20, l: 56 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const n = buckets.length;
  const maxTok = Math.max(...buckets.map(b => b.tokens || 0), 1);
  function niceMax(v) {
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const m = v / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
  }
  const yMax = niceMax(maxTok);
  const x = i => pad.l + (n <= 1 ? iw / 2 : i * (iw / (n - 1)));
  const y = v => pad.t + ih * (1 - v / yMax);
  const pts = buckets.map((b, i) => [x(i), y(b.tokens || 0)]);

  // 坐标文字回归 SVG 内（与图形一体）；viewBox=容器宽 → 1:1 真实 px
  const step = g === 'hour' ? 15 : g === 'day' ? 6 : 5;
  const offset = g === 'month' ? 1 : 0; // 月档 x 为日号 1-31
  const lastB = buckets[buckets.length - 1];
  let xticks = '';
  for (const b of buckets) {
    if ((b.x - offset) % step === 0 || b === lastB) {
      const i = buckets.indexOf(b);
      xticks += '<text x="' + x(i) + '" y="' + (H - 5) + '" text-anchor="middle">' + b.x + '</text>';
    }
  }
  let ygrid = '';
  for (let s = 0; s <= 4; s++) {
    const v = Math.round(yMax * s / 4);
    const yy = y(v);
    ygrid += '<line x1="' + pad.l + '" y1="' + yy + '" x2="' + (W - pad.r) + '" y2="' + yy + '" stroke="var(--if-border)" stroke-dasharray="3 3"/>' +
      '<text x="' + (pad.l - 8) + '" y="' + (yy + 4) + '" text-anchor="end">' + fmtTok(v) + '</text>';
  }
  const linePts = pts.map(p => p.map(v => Math.round(v * 100) / 100).join(',')).join(' ');
  const last = pts[pts.length - 1];
  const segs = pts.map(p => 'L ' + Math.round(p[0] * 100) / 100 + ' ' + Math.round(p[1] * 100) / 100);
  const area = 'M ' + pad.l + ' ' + (pad.t + ih) + ' ' + segs.join(' ') +
    ' L ' + (Math.round(last[0] * 100) / 100) + ' ' + (pad.t + ih) + ' Z';

  const totalTok = buckets.reduce((s, b) => s + (b.tokens || 0), 0);
  const totalReq = buckets.reduce((s, b) => s + (b.requests || 0), 0);

  return '<svg class="ov-curve-svg" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto">' +
    ygrid + xticks +
    '<path d="' + area + '" fill="var(--if-c-primary-soft)" stroke="none"/>' +
    '<polyline points="' + linePts + '" fill="none" stroke="var(--if-c-primary)" stroke-width="2"/>' +
    pts.map(p => '<circle cx="' + (Math.round(p[0] * 100) / 100) + '" cy="' + (Math.round(p[1] * 100) / 100) + '" r="2" fill="var(--if-c-primary)"/>').join('') +
    '</svg>' +
    '<div class="ov-curve-summary">合计 ' + fmtTok(totalTok) + ' tokens · ' + totalReq + ' 次请求</div>';
}

document.addEventListener('click', e => {
  const t = e.target.closest('.ov-g-btn');
  if (!t) return;
  document.querySelectorAll('.ov-g-btn').forEach(b => b.classList.remove('active'));
  t.classList.add('active');
  ovTokenGranularity = t.dataset.g;
  loadTokenCurve();
});


// ── Initialization (P0: migrated to store-driven) ──
function init() {
  toggleDeployForm();           // Collapse deploy form by default
  if (window.restoreTab) restoreTab();
  if (window.startPolling) startPolling();  // state.js: 3s fetch → store → render(bindings)
  load();                       // One-time: history, active svc, vLLM metrics
  loadModels();
  loadOverview();
  ovPerfTick();
  ovPerfStart();
  if (window.store && window.store.on) {
    store.on('models', loadOverview);
    store.on('status', loadOverview);
    store.on('gpu', loadOverview);
    store.on('cpu', loadOverview);
    store.on('mem', loadOverview);
    store.on('version', loadOverview);
  }
  loadLocalModels();
  loadUsage();
  loadTokenCurve();
  cloudLoadPresets();
  cloudLoadProviders();
  // v5.x: The 3s /api/snapshot poller is the only periodic refresh —
  // the old 5s load() / 30s loadUsage() intervals are gone. This
  // eliminates the parallel-fetch race that caused the "state gap".
}

// Init
window.addEventListener('DOMContentLoaded', init);

// ── v5.x: Panel auto-refresh on control-plane change ──
function refreshPanels() {
  load();
  loadModels();
  loadLocalModels();
  loadUsage();
  loadTokenCurve();
}
window.refreshPanels = refreshPanels;

// The 3s /api/snapshot poller emits sync_meta; when the control plane
// changed, re-render model panels so the UI tracks live state.
store.on('sync_meta', function(meta) {
  if (meta && meta.changed) refreshPanels();
});

// Top-bar manual refresh button (forces a fresh snapshot, bypassing 304)
async function refreshNow() {
  await store.forceRefresh();
  window.showToast('已刷新 ✓', 'ok');
}
window.refreshNow = refreshNow;

/* ── S3: Chat 推理 ── */
let _chatHistory = [];

function sendChat() {
  const input = document.getElementById('chatInput');
  const modelSel = document.getElementById('chatModel');
  const messages = document.getElementById('chatMessages');
  const stats = document.getElementById('chatStats');
  const btn = document.getElementById('chatSendBtn');
  const text = input.value.trim();
  if (!text) return;
  const model = modelSel.value;
  if (!model) { showToast('请先选择活跃模型', 'err'); return; }

  // User message
  input.value = '';
  _chatHistory.push({role:'user', content: text});
  renderChatMsg('user', text);

  // Loading indicator
  const loader = document.createElement('div');
  loader.className = 'chat-msg loading';
  loader.innerHTML = '<div class="chat-msg-role">Assistant</div><div class="chat-msg-bubble"></div>';
  messages.appendChild(loader);
  messages.scrollTop = messages.scrollHeight;
  btn.disabled = true;

  // Call /v1/chat/completions via proxy
  const maxTokens = parseInt(document.getElementById('chatMaxTokens').value, 10) || 1024;
  const temp = parseFloat(document.getElementById('chatTemp').value) || 0.7;

  const payload = {
    model: model,
    messages: _chatHistory.slice(-10),
    max_tokens: maxTokens,
    temperature: temp,
    stream: false
  };

  fetch('/v1/chat/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
    .then(r => r.json())
    .then(data => {
      loader.remove();
      if (data.choices && data.choices[0]) {
        const reply = data.choices[0].message.content;
        _chatHistory.push({role:'assistant', content: reply});
        renderChatMsg('assistant', reply);
        const usage = data.usage || {};
        stats.textContent = `Tokens: ${usage.prompt_tokens||'?'} in · ${usage.completion_tokens||'?'} out · ${usage.total_tokens||'?'} total`;
      } else {
        renderChatMsg('assistant', '⚠️ ' + (data.error?.message || '无响应'));
        stats.textContent = '';
      }
      btn.disabled = false;
    })
    .catch(err => {
      loader.remove();
      renderChatMsg('assistant', '⚠️ 请求失败: ' + err.message);
      btn.disabled = false;
    });
}

function renderChatMsg(role, content) {
  var div = document.createElement('div');
  div.className = 'chat-msg ' + role;
  div.innerHTML = '<div class="chat-msg-role">' + (role==='user'?'You':'Assistant') + '</div>' + 
    '<div class="chat-msg-bubble">' + esc(content) + '</div>';
  document.getElementById('chatMessages').appendChild(div);
  var msgs = document.getElementById('chatMessages');
  msgs.scrollTop = msgs.scrollHeight;
}

// Populate model dropdown from status
function updateChatModelSelect() {
  var sel = document.getElementById('chatModel');
  if (!sel) return;
  var currentVal = sel.value;  // save selection
  
  // Fetch all models, filter chat-capable types
  var chatTypes = ['llm', 'vl', 'omni'];
  function renderSelect(models) {
    if (!Array.isArray(models)) return;
    sel.innerHTML = '';
    var hasAny = false;

    // Local chat-capable models (all, not just active — IFF auto-switches)
    var localChat = models.filter(function(m) {
      return chatTypes.indexOf(m.model_type) >= 0;
    });
    if (localChat.length > 0) {
      var localGroup = document.createElement('optgroup');
      localGroup.label = '本地模型';
      localChat.forEach(function(m) {
        var opt = document.createElement('option');
        opt.value = m.name;
        opt.textContent = m.name + (m.active ? ' ●' : '');
        localGroup.appendChild(opt);
        hasAny = true;
      });
      sel.appendChild(localGroup);
    }

    // Cloud provider models
    if (window._cloudModelIds && window._cloudModelIds.length > 0) {
      var cloudGroup = document.createElement('optgroup');
      cloudGroup.label = '云端 Provider';
      window._cloudModelIds.forEach(function(m) {
        var opt = document.createElement('option');
        opt.value = m.id;
        opt.textContent = m.id + ' (' + m.provider + ')';
        cloudGroup.appendChild(opt);
        hasAny = true;
      });
      sel.appendChild(cloudGroup);
    }

    if (!hasAny) {
      sel.innerHTML = '<option value="">— 无可用模型 —</option>';
    }
    if (currentVal) { sel.value = currentVal; }
  }
  var storeModels = store.get('models');
  if (storeModels && storeModels.length > 0) {
    renderSelect(storeModels);
  } else {
    fetch('/models').then(function(r) { return r.json(); }).then(renderSelect).catch(function() {
      sel.innerHTML = '<option value="">— 加载失败 —</option>';
    });
  }
}

// Auto-update model dropdown
setTimeout(updateChatModelSelect, 1000);