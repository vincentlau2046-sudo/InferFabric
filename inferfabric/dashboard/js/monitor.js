/* InferFabric Console — Monitor tab (v2, Task 5)
 * 纯遥测、只读、零操作（spec §4.3）。
 *   - 3 ECharts: GPU vram+util 时间曲线 / Token prompt+completion 堆叠条 / 延迟 P50+P95 双线
 *   - 5 KPI: KV Cache / Seq Length / TPOT / TTFT / Throughput（GET /api/engine_metrics）
 *   - 2 表: 请求日志 + 切换历史（13px 紧凑）
 *   - 1 卡: 费用概览
 *
 * 数据源（全部 GET，只读）：
 *   - store /api/snapshot → metrics_24h request_log history gpu gpu_util active_services
 *   - window.__TOKEN_STATS__ → 按日聚合 prompt/completion tokens
 *   - GET /api/engine_metrics?model=<active> → 5 KPI 原始指标
 *
 * 窗口/粒度切换 = 客户端 display filter（不触达服务端状态变更）。
 *
 * 暴露：window.tabRenderers['tab-monitor'] = renderMonitor
 */
(function () {
  'use strict';

  var UI = window.UI;
  var store = window.store;
  if (!UI || !store) {
    console.warn('[monitor] UI/store not ready — deferring');
    return;
  }

  var IFCharts = window.IFCharts;
  var $ = function (id) { return document.getElementById(id); };

  /* ── 状态 ── */
  var _gpuWin = '24h';           // GPU 图表窗口 display filter
  var _tokenGran = 'hour';       // Token 图表粒度 display filter
  var _charts = { gpu: null, token: null, latency: null };
  var _chartsInit = false;

  // GPU 客户端时间序列 ring buffer（snapshot 轮询时积累）
  // 后端无历史 GPU 时间序列 API；此处客户端采样积累，页面打开后开始记录。
  var _gpuBuf = [];
  var _GPU_BUF_CAP = 2880;       // ~2.4h @3s 轮询；超出后移除最旧

  // 引擎指标节流（避免每 3s 轮询都打 /api/engine_metrics）
  var _engineCache = null;
  var _engineModel = null;
  var _lastEngineFetch = 0;
  var ENGINE_TTL = 15000;        // 15s

  /* ── 小工具 ── */
  function escHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function shortName(m) {
    if (!m) return '—';
    return String(m).split('/').pop();
  }

  function isMonitorActive() {
    var el = $('tab-monitor');
    return !!(el && el.classList.contains('active'));
  }

  function fmtTok(n) {
    if (n == null || isNaN(n)) return '0';
    n = Number(n);
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
    return String(n);
  }

  function showEmpty(id, show) {
    var el = $(id);
    if (el) el.style.display = show ? '' : 'none';
  }

  /* ── 图表初始化（null-check：IFCharts 缺 echarts 时返回 null） ── */
  function ensureCharts() {
    if (_chartsInit) return;
    _chartsInit = true;
    if (IFCharts && typeof IFCharts.create === 'function') {
      _charts.gpu = IFCharts.create('monGpuChart');
      _charts.token = IFCharts.create('monTokenChart');
      _charts.latency = IFCharts.create('monLatencyChart');
    }
    // null → 容器显示 empty state（IFCharts 已 log warning）
    if (!_charts.gpu) {
      var g = $('monGpuChart');
      if (g) g.innerHTML = '<div class="if-empty">图表库不可用</div>';
    }
    if (!_charts.token) {
      var t = $('monTokenChart');
      if (t) t.innerHTML = '<div class="if-empty">图表库不可用</div>';
    }
    if (!_charts.latency) {
      var l = $('monLatencyChart');
      if (l) l.innerHTML = '<div class="if-empty">图表库不可用</div>';
    }
  }

  /* ── 1. GPU 显存/利用率时间曲线 ──
   * 2 系列（VRAM% + Util%），单 y 轴 0-100%。客户端 ring buffer 采样积累。
   * 窗口 1h/24h/7d = display filter（过滤 ring buffer 回看范围）。 */
  function pushGpuSample() {
    var gpu = store.get('gpu') || {};
    var util = store.get('gpu_util') || {};
    var vramPct = gpu.pct;
    var utilPct = util.pct;
    if (vramPct == null && utilPct == null) return;
    _gpuBuf.push({
      ts: Date.now(),
      vram: vramPct != null ? +Number(vramPct).toFixed(1) : 0,
      util: utilPct != null ? +Number(utilPct).toFixed(1) : 0,
    });
    if (_gpuBuf.length > _GPU_BUF_CAP) _gpuBuf.shift();
  }

  function renderGpuChart() {
    ensureCharts();
    if (!_charts.gpu) return;

    // 采样由 sync_meta handler 统一负责（每次 snapshot 到达 push 一次），
    // 此处不重复采样，避免 tab 活跃时双倍写入。
    var winMs = { '1h': 3600000, '24h': 86400000, '7d': 604800000 }[_gpuWin] || 86400000;
    var cutoff = Date.now() - winMs;
    var pts = [];
    for (var i = 0; i < _gpuBuf.length; i++) {
      if (_gpuBuf[i].ts >= cutoff) pts.push(_gpuBuf[i]);
    }

    if (pts.length < 2) {
      showEmpty('monGpuEmpty', true);
      // 清空图表但保留坐标系
      IFCharts.update(_charts.gpu, {
        xAxis: { data: [] },
        yAxis: { max: 100 },
        series: [
          { type: 'line', name: 'VRAM %', data: [], areaStyle: { opacity: 0.08 } },
          { type: 'line', name: '利用率 %', data: [] },
        ],
      });
      return;
    }
    showEmpty('monGpuEmpty', false);

    var xs = [];
    var vram = [];
    var util = [];
    for (var j = 0; j < pts.length; j++) {
      var d = new Date(pts[j].ts);
      xs.push(d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }));
      vram.push(pts[j].vram);
      util.push(pts[j].util);
    }

    IFCharts.update(_charts.gpu, {
      xAxis: { data: xs },
      yAxis: { max: 100, axisLabel: { formatter: '{value}%' } },
      series: [
        { type: 'line', name: 'VRAM %', data: vram, areaStyle: { opacity: 0.08 } },
        { type: 'line', name: '利用率 %', data: util },
      ],
      dataZoom: [{ type: 'inside', start: 0, end: 100 }],
    });
  }

  /* ── 2. Token 用量：prompt/completion 堆叠条 ──
   * 2 系列（Prompt + Completion），单 y 轴。
   * 粒度 hour/day/month = display filter。
   *   - hour: snapshot request_log 按分钟分桶（最近 1h，60 桶）
   *   - day:  __TOKEN_STATS__ 按日（已有 prompt_tokens/generation_tokens 拆分）
   *   - month: __TOKEN_STATS__ 按月聚合 */
  function buildTokenData(gran) {
    if (gran === 'day') return buildTokenDay();
    if (gran === 'month') return buildTokenMonth();
    return buildTokenHour();
  }

  function buildTokenHour() {
    var logs = store.get('request_log') || [];
    var buckets = [];
    for (var i = 0; i < 60; i++) buckets.push({ p: 0, c: 0 });
    var now = Date.now();
    for (var i2 = 0; i2 < logs.length; i2++) {
      var l = logs[i2];
      if (!l || !l.timestamp) continue;
      var d = new Date(l.timestamp * 1000);
      var diff = now - d.getTime();
      if (diff < 0 || diff > 3600000) continue;
      var minAgo = Math.floor(diff / 60000);
      var idx = 59 - minAgo;
      if (idx >= 0 && idx < 60) {
        buckets[idx].p += (l.tokens_in || 0);
        buckets[idx].c += (l.tokens_out || 0);
      }
    }
    var xs = [];
    var prompt = [];
    var comp = [];
    for (var k = 0; k < 60; k++) {
      var dd = new Date(now - (59 - k) * 60000);
      xs.push(dd.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }));
      prompt.push(buckets[k].p);
      comp.push(buckets[k].c);
    }
    return { xs: xs, prompt: prompt, completion: comp };
  }

  function buildTokenDay() {
    var stats = window.__TOKEN_STATS__ || {};
    var keys = Object.keys(stats).sort();
    if (!keys.length) return { xs: [], prompt: [], completion: [] };
    var recent = keys.slice(-14);   // 最近 14 天
    var xs = [], prompt = [], comp = [];
    for (var i = 0; i < recent.length; i++) {
      var day = recent[i];
      var models = stats[day] || {};
      var p = 0, c = 0;
      for (var m in models) {
        if (!models[m]) continue;
        p += (models[m].prompt_tokens || 0);
        c += (models[m].generation_tokens || 0);
      }
      // label = MM-DD
      var label = day.length >= 10 ? day.slice(5) : day;
      xs.push(label);
      prompt.push(p);
      comp.push(c);
    }
    return { xs: xs, prompt: prompt, completion: comp };
  }

  function buildTokenMonth() {
    var stats = window.__TOKEN_STATS__ || {};
    var keys = Object.keys(stats).sort();
    if (!keys.length) return { xs: [], prompt: [], completion: [] };
    var months = {};   // YYYY-MM → {p, c}
    for (var i = 0; i < keys.length; i++) {
      var day = keys[i];
      var ym = day.length >= 7 ? day.slice(0, 7) : day;
      if (!months[ym]) months[ym] = { p: 0, c: 0 };
      var models = stats[day] || {};
      for (var m in models) {
        if (!models[m]) continue;
        months[ym].p += (models[m].prompt_tokens || 0);
        months[ym].c += (models[m].generation_tokens || 0);
      }
    }
    var mkeys = Object.keys(months).sort();
    var xs = [], prompt = [], comp = [];
    for (var j = 0; j < mkeys.length; j++) {
      xs.push(mkeys[j]);
      prompt.push(months[mkeys[j]].p);
      comp.push(months[mkeys[j]].c);
    }
    return { xs: xs, prompt: prompt, completion: comp };
  }

  function renderTokenChart() {
    ensureCharts();
    if (!_charts.token) return;

    var data = buildTokenData(_tokenGran);
    var hasData = false;
    for (var i = 0; i < data.prompt.length; i++) {
      if (data.prompt[i] > 0 || data.completion[i] > 0) { hasData = true; break; }
    }
    showEmpty('monTokenEmpty', !hasData);

    IFCharts.update(_charts.token, {
      xAxis: { data: data.xs, boundaryGap: true },
      yAxis: { axisLabel: { formatter: function (v) { return fmtTok(v); } } },
      series: [
        { type: 'bar', name: 'Prompt', stack: 'tok', data: data.prompt },
        { type: 'bar', name: 'Completion', stack: 'tok', data: data.completion },
      ],
    });
  }

  /* ── 3. 延迟 P50 / P95 双线 ──
   * 2 系列（TTFT P50 + TTFT P95），单 y 轴（ms）。
   * 数据源：metrics_24h.models[model].ttft_p50 / ttft_p95（按模型 x 轴）。
   * 注：后端仅计算 p50/p95/p99，无 p90；用 p95 代替 spec 所述 p90（最近可用分位）。 */
  function renderLatencyChart() {
    ensureCharts();
    if (!_charts.latency) return;

    var m = store.get('metrics_24h') || {};
    var models = m.models || {};
    var names = Object.keys(models);

    if (!names.length) {
      showEmpty('monLatencyEmpty', true);
      IFCharts.update(_charts.latency, {
        xAxis: { data: [] },
        yAxis: { axisLabel: { formatter: '{value} ms' } },
        series: [
          { type: 'line', name: 'P50', data: [] },
          { type: 'line', name: 'P95', data: [] },
        ],
      });
      return;
    }
    showEmpty('monLatencyEmpty', false);

    var xs = [];
    var p50 = [];
    var p95 = [];
    for (var i = 0; i < names.length; i++) {
      var vm = models[names[i]] || {};
      xs.push(shortName(names[i]));
      p50.push(vm.ttft_p50 != null ? vm.ttft_p50 : null);
      p95.push(vm.ttft_p95 != null ? vm.ttft_p95 : null);
    }

    IFCharts.update(_charts.latency, {
      xAxis: { data: xs, boundaryGap: true },
      yAxis: { axisLabel: { formatter: '{value} ms' } },
      series: [
        { type: 'line', name: 'P50', data: p50 },
        { type: 'line', name: 'P95', data: p95 },
      ],
    });
  }

  /* ── 4. 五联 KPI ──
   * GET /api/engine_metrics?model=<active> → kv_cache_usage_perc / seq_length /
   * tpot_seconds.mean / ttft_seconds.mean / throughput */
  function kpiTile(label, val, tip) {
    return '<div class="kpi" title="' + escHtml(tip) + '">' +
      '<span class="kpi-label">' + escHtml(label) + '</span>' +
      '<span class="kpi-val mono">' + escHtml(val) + '</span>' +
    '</div>';
  }

  function drawKpis(data) {
    var el = $('monKpis');
    if (!el) return;
    if (!data) {
      el.innerHTML = '<div class="if-empty">引擎指标暂不可用</div>';
      return;
    }
    // 休眠模型或无指标数据
    if (data.sleep_state === 0 && data.kv_cache_usage_perc == null &&
        data.seq_length == null && data.throughput == null &&
        data.ttft_seconds == null && data.tpot_seconds == null) {
      el.innerHTML = '<div class="if-empty">模型休眠中，无实时指标 — 到推理 TAB 唤醒</div>';
      return;
    }

    var kv = data.kv_cache_usage_perc;
    var seq = data.seq_length;
    var tpot = data.tpot_seconds;
    var ttft = data.ttft_seconds;
    var thr = data.throughput;

    el.innerHTML =
      '<div class="mon-kpi-grid">' +
        kpiTile('KV Cache', kv != null ? Number(kv).toFixed(1) + '%' : '—',
          'KV 缓存占用率（来自引擎 /metrics）') +
        kpiTile('Seq Length', seq != null ? UI.fmtNum(seq) : '—',
          '平均请求序列长度（prompt + generation tokens）') +
        kpiTile('TPOT', tpot && tpot.mean != null ? (tpot.mean * 1000).toFixed(1) + 'ms' : '—',
          'Time Per Output Token — 每输出 token 生成耗时') +
        kpiTile('TTFT', ttft && ttft.mean != null ? (ttft.mean * 1000).toFixed(1) + 'ms' : '—',
          'Time To First Token — 首 token 延迟') +
        kpiTile('Throughput', thr != null ? UI.fmtNum(Math.round(thr)) + ' tok/s' : '—',
          'EMA 平滑吞吐（tokens/s）') +
      '</div>';
  }

  function renderKpis() {
    var el = $('monKpis');
    if (!el) return;

    var now = Date.now();
    if (_engineCache && now - _lastEngineFetch < ENGINE_TTL) {
      drawKpis(_engineCache);
      return;
    }

    var active = store.get('active_services') || [];
    if (!active.length) {
      _engineCache = null;
      _engineModel = null;
      el.innerHTML = '<div class="if-empty">无活跃模型 — 到推理 TAB 启动一个</div>';
      var mLabel = $('monKpiModel');
      if (mLabel) mLabel.textContent = '';
      return;
    }
    var model = active[0];
    var mLabel = $('monKpiModel');
    if (mLabel) mLabel.textContent = shortName(model);

    // 模型未变且有缓存 → 直接用
    if (_engineModel === model && _engineCache && now - _lastEngineFetch < ENGINE_TTL) {
      drawKpis(_engineCache);
      return;
    }

    UI.skeleton(el, 3);
    // GET only — no method specified = GET (read-only constraint)
    fetch('/api/engine_metrics?model=' + encodeURIComponent(model), {
      cache: 'no-store',
    })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        _engineCache = data;
        _engineModel = model;
        _lastEngineFetch = Date.now();
        drawKpis(data);
      })
      .catch(function () {
        el.innerHTML = '<div class="if-empty">引擎指标暂不可用</div>';
      });
  }

  /* ── 5. 请求日志表 ── */
  function renderLogTable() {
    var el = $('monLogTable');
    if (!el) return;
    var logs = store.get('request_log') || [];
    if (!logs.length) {
      el.innerHTML = '<div class="if-empty">暂无请求日志 — 经代理发起请求后在此记录</div>';
      return;
    }
    var rows = '';
    for (var i = 0; i < logs.length; i++) {
      var l = logs[i] || {};
      var ts = l.timestamp
        ? new Date(l.timestamp * 1000).toLocaleTimeString('zh-CN',
            { hour: '2-digit', minute: '2-digit', second: '2-digit' })
        : '—';
      var stCls = (l.status || 0) < 400 ? 'ok' : 'crit';
      var tokIn = UI.fmtNum(l.tokens_in) || '0';
      var tokOut = UI.fmtNum(l.tokens_out) || '0';
      var ttft = l.ttft_ms != null ? l.ttft_ms.toFixed(0) + 'ms' : '—';
      var dur = l.duration_ms != null ? l.duration_ms.toFixed(0) + 'ms' : '—';
      rows += '<tr>' +
        '<td class="mono">' + escHtml(ts) + '</td>' +
        '<td>' + escHtml(shortName(l.model)) + '</td>' +
        '<td><span class="badge ' + stCls + '">' + escHtml(l.status) + '</span></td>' +
        '<td class="mono num">' + escHtml(tokIn + ' / ' + tokOut) + '</td>' +
        '<td class="mono num">' + escHtml(ttft) + '</td>' +
        '<td class="mono num">' + escHtml(dur) + '</td>' +
      '</tr>';
    }
    el.innerHTML =
      '<table class="if-table mon-tbl">' +
        '<thead><tr><th>时间</th><th>模型</th><th>状态</th>' +
        '<th>Tokens in/out</th><th>TTFT</th><th>耗时</th></tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>';
    var tsEl = $('monLogTs');
    if (tsEl) tsEl.textContent = new Date().toLocaleTimeString('zh-CN',
      { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  /* ── 6. 切换历史表 ── */
  function renderHistTable() {
    var el = $('monHistTable');
    if (!el) return;
    var hist = store.get('history') || [];
    if (!hist.length) {
      el.innerHTML = '<div class="if-empty">暂无切换历史 — 切换模型后在此记录</div>';
      return;
    }
    var rows = '';
    for (var i = 0; i < hist.length; i++) {
      var h = hist[i] || {};
      var ts = h.timestamp
        ? new Date(h.timestamp * 1000).toLocaleString('zh-CN',
            { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
        : '—';
      var from = shortName(h.from);
      var to = shortName(h.to);
      var dur = h.duration != null ? UI.fmtDur(h.duration) : '—';
      var st = h.status || '';
      var stCls = st === 'ok' ? 'ok' : (st === 'error' || st === 'fail' ? 'crit' : 'info');
      rows += '<tr>' +
        '<td class="mono">' + escHtml(ts) + '</td>' +
        '<td>' + escHtml(from) + '</td>' +
        '<td>' + escHtml(to) + '</td>' +
        '<td class="mono">' + escHtml(dur) + '</td>' +
        '<td>' + (st ? '<span class="badge ' + stCls + '">' + escHtml(st) + '</span>' : '—') + '</td>' +
      '</tr>';
    }
    el.innerHTML =
      '<table class="if-table mon-tbl">' +
        '<thead><tr><th>时间</th><th>From</th><th>To</th><th>耗时</th><th>状态</th></tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>';
  }

  /* ── 7. 费用概览卡 ── */
  function renderCostCard() {
    var el = $('monCostBody');
    if (!el) return;
    var m = store.get('metrics_24h') || {};
    var total = m.cost_yuan || 0;
    var models = m.models || {};
    var rows = '';
    var entries = [];
    for (var name in models) {
      if (models[name] && models[name].cost_yuan > 0) {
        entries.push({ name: name, cost: models[name].cost_yuan });
      }
    }
    entries.sort(function (a, b) { return b.cost - a.cost; });

    if (!entries.length) {
      el.innerHTML =
        '<div class="mon-cost-total"><span class="val">¥' + total.toFixed(4) + '</span></div>' +
        '<div class="if-empty">暂无费用数据 — 产生用量后在此显示</div>';
      return;
    }
    for (var i = 0; i < entries.length; i++) {
      rows += '<div class="mon-cost-row">' +
        '<span class="model" title="' + escHtml(entries[i].name) + '">' +
          escHtml(shortName(entries[i].name)) + '</span>' +
        '<span class="amt mono">¥' + entries[i].cost.toFixed(4) + '</span>' +
      '</div>';
    }
    el.innerHTML =
      '<div class="mon-cost-total"><span class="muted">24h 总计</span>' +
        '<span class="val mono">¥' + total.toFixed(4) + '</span></div>' +
      rows;
  }

  /* ── 渲染入口 ── */
  function renderMonitor() {
    renderGpuChart();
    renderTokenChart();
    renderLatencyChart();
    renderKpis();
    renderLogTable();
    renderHistTable();
    renderCostCard();
  }
  window.tabRenderers['tab-monitor'] = renderMonitor;

  /* ── 订阅：sync_meta → 仅 monitor tab 活跃时刷新 ──
   * GPU ring buffer 在每次 sync_meta（snapshot 到达）时积累样本。
   * 无条件 push（不限定 tab 活跃、无 one-shot 种子标志）——保证切回
   * monitor tab 时 ring buffer 已有跨会话时长的连续数据。 */
  store.on('sync_meta', function () {
    if (store.get('gpu')) pushGpuSample();
    if (isMonitorActive()) renderMonitor();
  });

  store.on('tab_active', function (tab) {
    if (tab === 'tab-monitor') {
      // 切到 monitor tab：立即渲染（charts 可能需要 init）
      renderMonitor();
    }
  });

  /* ── 事件委托：窗口/粒度切换（display filter，不触达服务端） ── */
  var tabEl = $('tab-monitor');
  if (tabEl) {
    tabEl.addEventListener('click', function (ev) {
      var btn = ev.target.closest('.mon-seg-btn');
      if (!btn || !tabEl.contains(btn)) return;
      var seg = btn.closest('.mon-seg');
      if (!seg) return;
      var segType = seg.getAttribute('data-seg');

      // 更新 active 态
      seg.querySelectorAll('.mon-seg-btn').forEach(function (b) {
        b.classList.remove('active');
      });
      btn.classList.add('active');

      if (segType === 'win') {
        var win = btn.getAttribute('data-win');
        if (win && win !== _gpuWin) {
          _gpuWin = win;
          renderGpuChart();
        }
      } else if (segType === 'gran') {
        var gran = btn.getAttribute('data-gran');
        if (gran && gran !== _tokenGran) {
          _tokenGran = gran;
          renderTokenChart();
        }
      }
    });
  }

  /* ── 主题变更：IFCharts 自动 dispose+重建，但我们需要重新注入数据 ── */
  if (IFCharts && typeof IFCharts.onThemeChange === 'function') {
    IFCharts.onThemeChange(function () {
      // 重建后实例已更新，重新渲染所有图表注入数据
      if (isMonitorActive()) {
        renderGpuChart();
        renderTokenChart();
        renderLatencyChart();
      }
    });
  }
})();
