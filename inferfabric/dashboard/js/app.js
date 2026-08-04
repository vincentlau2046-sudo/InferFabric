// ── XSS-safe helpers ──
function esc(v) { if(v==null)return''; const d=document.createElement('div'); d.textContent=String(v); return d.innerHTML; }
function escAttr(v) { return String(v==null?'':v).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

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
      card.className = 'preset-card';
      card.dataset.presetId = p.id;
      card.style.cssText = 'cursor:pointer;display:flex;flex-direction:column;align-items:center;padding:10px 14px;border-radius:10px;border:1.5px solid var(--border);background:var(--bg);transition:all .2s;min-width:72px';
      const iconSpan = document.createElement('span');
      iconSpan.style.fontSize = '22px';
      iconSpan.textContent = p.icon;
      const nameSpan = document.createElement('span');
      nameSpan.style.cssText = 'font-size:11px;margin-top:4px;color:var(--text2);white-space:nowrap';
      nameSpan.textContent = p.display_name;
      card.appendChild(iconSpan);
      card.appendChild(nameSpan);
      card.addEventListener('click', () => cloudSelectPreset(p.id));
      grid.appendChild(card);
    });
  } catch(e) { document.getElementById('presetGrid').textContent = '加载失败'; }
}

function cloudSelectPreset(id) {
  _selectedPreset = _cloudPresets.find(p => p.id === id);
  if (!_selectedPreset) return;
  // Highlight selected card
  document.querySelectorAll('.preset-card').forEach(c => { c.style.borderColor = 'var(--border)'; c.style.background = 'var(--bg)'; });
  const card = document.getElementById('preset-' + id);
  if (card) { card.style.borderColor = 'var(--primary)'; card.style.background = 'var(--bg-card)'; }
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
  document.querySelectorAll('.preset-card').forEach(c => { c.style.borderColor = 'var(--border)'; c.style.background = 'var(--bg)'; });
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

function cloudLoadModels(data) {
  const models = (data && data.models) || [];
  document.getElementById('cloudModelCount').textContent = models.length + ' 个模型';
  if (!models.length) {
    document.getElementById('cloudModelsList').innerHTML = '<div class="svc-empty">暂无模型 — 点击 Provider 的 🔍 发现</div>';
    return;
  }
  let html = '';
  models.sort((a,b) => (b.discovered_at||0) - (a.discovered_at||0));
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

// Load cloud data when switching to cloud tab
const origSwitchTab = switchTab;
switchTab = function(tabId) {
  origSwitchTab(tabId);
  if (tabId === 'tab-cloud') { cloudLoadPresets(); cloudLoadProviders(); }
};
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
  // Version from API (PR-17)
  const ver=sys.version;
  if(ver){const ve=document.getElementById('navVer');if(ve)ve.textContent='v'+ver;}

  // History
  const hBody=document.getElementById('hBody');
  if(!hist||!hist.length){hBody.innerHTML='<div style="text-align:center;padding:20px;color:var(--text4);font-size:14px">暂无记录</div>';}
  else{hBody.innerHTML=hist.slice(0,12).map(h=>{
    const t=h.timestamp?new Date(h.timestamp+'Z'):new Date();
    const ts=t.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    const d=h.duration!=null?h.duration.toFixed(1)+'s':'—';
    const st=h.status==='ok'?'<span class="h-ok">✓</span>':'<span class="h-err">✗</span>';
    return '<div class="hrow"><span class="h-time">'+ts+'</span><span class="h-from">'+esc(h.from||'—')+'</span><span class="h-arrow">→</span><span class="h-to">'+esc(h.to)+'</span><span class="h-dur">'+d+'</span><span>'+st+'</span></div>';
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
      btns+='<button class="btn-card stop" onclick="event.stopPropagation();doRelease(\''+escAttr(m.name)+'\','+(m.mode==='exclusive')+')">释放</button>';
      if(isVllm) btns+='<button class="btn-card start" onclick="event.stopPropagation();doSleep(\''+escAttr(m.name)+'\')">休眠</button>';
    }else if(sleeping){
      btns+='<button class="btn-card stop" onclick="event.stopPropagation();doRelease(\''+escAttr(m.name)+'\','+(m.mode==='exclusive')+')">释放</button>';
      if(isVllm) btns+='<button class="btn-card start" onclick="event.stopPropagation();doWake(\''+escAttr(m.name)+'\')">唤醒</button>';
    }else{
      btns+='<button class="btn-card start" onclick="event.stopPropagation();doSwitch(\''+escAttr(m.name)+'\')">启动</button>';
    }

    // Specs row: framework + model type + context window + quantization — always 4 slots for alignment
    const fwIcons = { vllm:'🔥', ollama:'🦙', ollama_cpp:'📦', comfyui:'🖼️' };
    const fwLabels = { vllm:'vLLM', ollama:'Ollama', ollama_cpp:'ollama.cpp', comfyui:'ComfyUI' };
    const framework = fwLabels[m.type] || m.type;
    const fwIcon = fwIcons[m.type] || '📦';
    const ctxStr = m.context_window ? (m.context_window >= 1024 ? (m.context_window/1024).toFixed(0)+'K ctx' : m.context_window+' ctx') : '';
    // Icon/label by model_type (not modality) — ocr vs vl both → text-vision but need different icons
    const mtIcon = { llm:'🧠', vl:'👁', omni:'🌐', ocr:'📄', aigc:'✨', embedding:'📊', rerank:'🔄', infra:'⚙️', tts:'🔊', asr:'🎤' };
    const mtLabel = { llm:'LLM', vl:'VL', omni:'Omni', ocr:'OCR', aigc:'AIGC', embedding:'Embed', rerank:'Rerank', infra:'Infra', tts:'TTS', asr:'ASR' };
    // badge 文案：modeBadge 取值 excl/shrd/free，对应独占/共享/空闲
    const modeLabel = { excl:'独占', shrd:'共享', free:'空闲' };
    const modality = m.modality || 'text';
    const mt = m.model_type || 'llm';
    // Always render 4 spec slots; missing ones get hidden placeholders for alignment
    const specSlots = [];
    specSlots.push('<span class="spec-tag">'+fwIcon+' '+framework+'</span>');
    if(mtIcon[mt]) specSlots.push('<span class="spec-tag">'+mtIcon[mt]+' '+(mtLabel[mt]||mt)+'</span>');
    else specSlots.push('<span class="spec-tag" style="visibility:hidden">—</span>');
    if(ctxStr) specSlots.push('<span class="spec-tag">📐 '+ctxStr+'</span>');
    else specSlots.push('<span class="spec-tag" style="visibility:hidden">—</span>');
    if(m.quantization) specSlots.push('<span class="spec-tag">⚡ '+m.quantization+'</span>');
    else specSlots.push('<span class="spec-tag" style="visibility:hidden">—</span>');
    const specs = specSlots.join('');

    return '<div class="'+cls+'" id="sw-'+m.name+'">'+
      '<div class="model-top">'+
        '<div class="model-dot"></div>'+
        '<div class="model-info"><div class="model-name">'+esc(m.name)+'</div>'+
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
  finally { sw=false; }
  await Promise.all([load(), loadModels(), loadLocalModels()]);
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