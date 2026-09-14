/* R10: 异常看板 — 每 10s 轮询 /api/anomalies */
(function() {
  const POLL_MS = 10000;
  let timer = null;

  const SEV_CLASS = {info:'sev-info',warning:'sev-warning',error:'sev-error',critical:'sev-critical'};
  const SEV_LABEL = {info:'🟢 info',warning:'🟡 warning',error:'🔴 error',critical:'⚫ critical'};

  function render(e) {
    const d = new Date(e.ts*1000);
    const time = d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    return '<div class="anomaly-row">'
      + '<span class="anomaly-time">'+time+'</span>'
      + '<span class="anomaly-code">'+e.status_code+'</span>'
      + '<span class="anomaly-model">'+(e.model||'<empty>')+'</span>'
      + '<span class="anomaly-sev '+ (SEV_CLASS[e.severity]||'') +'">'+(SEV_LABEL[e.severity]||e.severity)+'</span>'
      + '<span class="anomaly-msg">'+escapeHtml(e.message)+'</span>'
      + '</div>'
      + '<div class="anomaly-cause">'+escapeHtml(e.possible_cause)+'</div>';
  }

  function escapeHtml(s) {
    if (!s) return '';
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
            .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  async function load() {
    const cat = document.getElementById('anomaly-category-filter').value;
    const sev = document.getElementById('anomaly-severity-filter').value;
    let url = '/api/anomalies?limit=200';
    if (cat) url += '&category='+cat;
    if (sev) url += '&severity='+sev;
    try {
      const data = await (await fetch(url)).json();
      const list = document.getElementById('anomaly-event-list');
      const cnt = document.getElementById('anomaly-count');
      const badge = document.getElementById('anomaly-count-badge');
      if (!data.events || data.events.length===0) {
        list.innerHTML = '<p style="color:var(--text3);padding:1.5em 0;text-align:center">暂无异常事件 ✔</p>';
        if (cnt) cnt.textContent = '';
        if (badge) { badge.style.display = 'none'; }
        return;
      }
      list.innerHTML = data.events.map(render).join('');
      if (cnt) cnt.textContent = data.count + ' 条';
      if (badge) { badge.textContent = data.count; badge.style.display = ''; }
    } catch(e) {
      document.getElementById('anomaly-event-list').innerHTML =
        '<p style="color:var(--text3);padding:1em 0;text-align:center">加载失败: '+escapeHtml(e.message)+'</p>';
    }
  }

  window.store.on('tab_active', function(val) {
    if (val === 'tab-anomaly') {
      load();
      timer = setInterval(load, POLL_MS);
    } else {
      if (timer) { clearInterval(timer); timer = null; }
    }
  });

  document.addEventListener('change', function(e) {
    if (e.target.id==='anomaly-category-filter' || e.target.id==='anomaly-severity-filter') {
      load();
    }
  });
})();