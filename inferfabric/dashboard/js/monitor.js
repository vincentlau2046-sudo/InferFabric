/* InferFabric Console — Monitor tab (v2, Task 5)
 * 纯遥测、只读、零操作（spec §4.3）。
 *   - 4 ECharts: GPU vram+util 时间曲线 / Token prompt+completion 堆叠条 /
 *     TTFT/TPOT 双卡趋势折线（v6.0 时间轴 × 逐模型分色，取代模型条形小倍数）
 *   - 6 KPI（2 行 × 3 列）: KV Cache / Batch Size / Seq Length / TPOT(ms) / TTFT(s) / Throughput
 *     （GET /api/engine_metrics）
 *   - 2 表: 请求日志 + 切换历史（13px 紧凑）
 *   - 1 卡: 费用概览
 *
 * 数据源（全部 GET，只读）：
 *   - store /api/snapshot → metrics_24h request_log history gpu gpu_util active_services
 *     + token_stats（双 scope {local,cloud}，DB 驱动、引擎无关 → 天/月图实时）
 *   - GET /api/token-curve?granularity=hour → 小时图 60 分钟桶（local/cloud 双 scope）
 *   - window.__TOKEN_STATS__ → 天/月图兜底（代理启动时烘焙的双 scope 快照）
 *   - GET /api/engine_metrics?model=<active> → 6 KPI 原始指标
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
  var _charts = { gpu: null, tokenLocal: null, tokenCloud: null, ttft: null, tpot: null };
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

  // Token 小时图节流 + 数据源切换（方案 B）：
  // 旧版复用 snapshot 的 request_log（limit=50）→ 重流量下只画 ~4 根柱子。
  // 改为单独 fetch /api/token-curve?granularity=hour（服务端 60 桶聚合 + limit=100000）。
  // local[i] = idx i（i=0 最旧 59 分钟前、i=59 最新），含 prompt/completion 拆分。
  var _tokenHourCache = null;
  var _lastTokenHourFetch = 0;
  var TOKEN_HOUR_TTL = 15000;    // 15s — 同 ENGINE_TTL

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
      _charts.tokenLocal = IFCharts.create('monTokenLocalChart');
      _charts.tokenCloud = IFCharts.create('monTokenCloudChart');
      _charts.ttft = IFCharts.create('monTtftChart');
      _charts.tpot = IFCharts.create('monTpotChart');
    }
    // null → 容器显示 empty state（IFCharts 已 log warning）
    if (!_charts.gpu) {
      var g = $('monGpuChart');
      if (g) g.innerHTML = '<div class="if-empty">图表库不可用</div>';
    }
    if (!_charts.tokenLocal) {
      var tl = $('monTokenLocalChart');
      if (tl) tl.innerHTML = '<div class="if-empty">图表库不可用</div>';
    }
    if (!_charts.tokenCloud) {
      var tc = $('monTokenCloudChart');
      if (tc) tc.innerHTML = '<div class="if-empty">图表库不可用</div>';
    }
    if (!_charts.ttft) {
      var tt = $('monTtftChart');
      if (tt) tt.innerHTML = '<div class="if-empty">图表库不可用</div>';
    }
    if (!_charts.tpot) {
      var tp = $('monTpotChart');
      if (tp) tp.innerHTML = '<div class="if-empty">图表库不可用</div>';
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
   *   - hour: /api/token-curve?granularity=hour（服务端 60 分钟桶 + limit=100000）
   *         — 不再用 snapshot request_log（limit=50，重流量下只画 ~4 根柱子）
   *   - day:  __TOKEN_STATS__ 按日（已有 prompt_tokens/generation_tokens 拆分）
   *   - month: __TOKEN_STATS__ 按月聚合 */
  function buildTokenData(gran, scope) {
    if (gran === 'day') return buildTokenDay(scope);
    if (gran === 'month') return buildTokenMonth(scope);
    return buildTokenHour(scope);
  }

  function buildTokenHour(scope) {
    // 数据源：/api/token-curve?granularity=hour（服务端 60 桶 + limit=100000）。
    // 响应含 {local:[...], cloud:[...]} 两个 scope 的桶数组，本地/云端两张图各取一份。
    var now = Date.now();

    // 同步返回缓存（命中 TTL）；否则启动异步 fetch 并返回上次缓存或全零（不阻塞渲染）。
    if (_tokenHourCache && (now - _lastTokenHourFetch) < TOKEN_HOUR_TTL) {
      return _tokenHourFromBuckets((_tokenHourCache[scope]) || [], now);
    }
    if (!_tokenHourFetchInflight) {
      _tokenHourFetchInflight = true;
      fetch('/api/token-curve?granularity=hour', { cache: 'no-store' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          // 保留 local + cloud 两个 scope（供本地/云端两张图各自取用）
          _tokenHourCache = data || {};
          _lastTokenHourFetch = Date.now();
        })
        .catch(function (e) {
          console.warn('[monitor] token-curve fetch failed:', e);
          // 失败不清缓存（保留下次可用旧值）；无缓存则置空
          _lastTokenHourFetch = Date.now();
        })
        .then(function () {
          _tokenHourFetchInflight = false;
          if (isMonitorActive()) renderTokenChart();
        });
    }
    return _tokenHourFromBuckets((_tokenHourCache && _tokenHourCache[scope]) || [], now);
  }

  // 把 token-curve 的 local 桶数组（idx 0=最旧→59=最新）转成图表数据。
  // 无缓存时返回 60 个零桶（空图 + empty state）。
  var _tokenHourFetchInflight = false;
  function _tokenHourFromBuckets(local, now) {
    var xs = [];
    var prompt = [];
    var comp = [];
    for (var k = 0; k < 60; k++) {
      var dd = new Date(now - (59 - k) * 60000);
      xs.push(dd.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }));
      var b = (local && local[k]) || {};
      prompt.push(b.prompt || 0);
      comp.push(b.completion || 0);
    }
    return { xs: xs, prompt: prompt, completion: comp };
  }

  /* 按天/按月数据源：优先 store 里的 token_stats（/api/snapshot 每 3s 轮询、
   * 后端 DB 驱动、引擎无关、双 scope local/cloud），回退到 head 注入的
   * window.__TOKEN_STATS__（同样双 scope，代理启动时烘焙）。两者都返回
   * {local:{date:{model:bucket}}, cloud:{...}}。 */
  function _tokenStatsSource() {
    var live = store.get('token_stats');
    if (live && (live.local || live.cloud)) return live;
    var baked = window.__TOKEN_STATS__;
    if (baked && (baked.local || baked.cloud)) return baked;
    return {};
  }

  function buildTokenDay(scope) {
    var stats = _tokenStatsSource();
    var s = stats[scope] || {};
    var keys = Object.keys(s).sort();
    if (!keys.length) return { xs: [], prompt: [], completion: [] };
    var recent = keys.slice(-30);   // 最近 30 天（柱状图标准化为 30 天）
    var xs = [], prompt = [], comp = [];
    for (var i = 0; i < recent.length; i++) {
      var day = recent[i];
      var models = s[day] || {};
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

  function buildTokenMonth(scope) {
    var stats = _tokenStatsSource();
    var s = stats[scope] || {};
    var keys = Object.keys(s).sort();
    if (!keys.length) return { xs: [], prompt: [], completion: [] };
    var months = {};   // YYYY-MM → {p, c}
    for (var i = 0; i < keys.length; i++) {
      var day = keys[i];
      var ym = day.length >= 7 ? day.slice(0, 7) : day;
      if (!months[ym]) months[ym] = { p: 0, c: 0 };
      var models = s[day] || {};
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

  /* 缓存命中率（Cache Hit Rate）= 缓存命中 prompt tokens / 总 prompt tokens。
   * 双协议统一口径：OpenAI 系取 prompt_tokens_details.cached_tokens（计入
   * prompt_tokens）；Anthropic 系取 cache_read_input_tokens（creation 计入
   * 总量、不计命中）。窗口随当前粒度：hour → token-curve 60 桶 cached/prompt；
   * day → 最近 30 天 token_stats；month → 全部 token_stats（30 天留存）。 */
  function _scopeCacheHitRate(scope) {
    var p = 0, c = 0, i, k;
    if (_tokenGran === 'hour') {
      var buckets = (_tokenHourCache && _tokenHourCache[scope]) || [];
      for (k = 0; k < buckets.length; k++) {
        var b = buckets[k] || {};
        p += b.prompt || 0;
        c += b.cached || 0;
      }
      return p > 0 ? c / p : null;
    }
    var stats = _tokenStatsSource();
    var s = stats[scope] || {};
    var keys = Object.keys(s).sort();
    if (_tokenGran === 'day') keys = keys.slice(-30);
    for (i = 0; i < keys.length; i++) {
      var models = s[keys[i]] || {};
      for (var m in models) {
        if (!models[m]) continue;
        p += models[m].prompt_tokens || 0;
        c += models[m].prompt_tokens_cached || 0;
      }
    }
    return p > 0 ? c / p : null;
  }

  function renderCacheHitBadges() {
    var pairs = [['monTokenLocalCacheHit', 'local'],
                 ['monTokenCloudCacheHit', 'cloud']];
    for (var i = 0; i < pairs.length; i++) {
      var el = $(pairs[i][0]);
      if (!el) continue;
      var rate = _scopeCacheHitRate(pairs[i][1]);
      el.textContent = rate == null
        ? 'Cache Hit Rate —'
        : 'Cache Hit Rate ' + (rate * 100).toFixed(1) + '%';
    }
  }

  /* 本地 / 云端两张图：同一粒度下各渲染一张（prompt/completion 堆叠条）。 */
  function renderTokenChart() {
    ensureCharts();
    if (!_charts.tokenLocal || !_charts.tokenCloud) return;
    renderCacheHitBadges();

    var scopes = [
      { chart: _charts.tokenLocal, empty: 'monTokenLocalEmpty', scope: 'local' },
      { chart: _charts.tokenCloud, empty: 'monTokenCloudEmpty', scope: 'cloud' },
    ];
    for (var i = 0; i < scopes.length; i++) {
      var sc = scopes[i];
      var data = buildTokenData(_tokenGran, sc.scope);
      var hasData = false;
      for (var j = 0; j < data.prompt.length; j++) {
        if (data.prompt[j] > 0 || data.completion[j] > 0) { hasData = true; break; }
      }
      showEmpty(sc.empty, !hasData);
      IFCharts.update(sc.chart, {
        xAxis: { data: data.xs, boundaryGap: true },
        yAxis: { axisLabel: { formatter: function (v) { return fmtTok(v); } } },
        series: [
          { type: 'bar', name: 'Prompt', stack: 'tok', data: data.prompt },
          { type: 'bar', name: 'Completion', stack: 'tok', data: data.completion },
        ],
      });
    }
  }

  /* ── 3. 模型延迟趋势：时间轴 × 逐模型分色折线（v6.0 取代模型条形双卡）──
   * 共享控制条：窗口 1h/24h/7d（latwin，单条不再镜像）+ 分位 P50/P50+P95（latq）
   * + 模型 chip 选择器（请求数前 5，默认选中前 4；颜色按 rank 定，chip 与图同色）。
   * 数据源：GET /api/latency?window=（时间分桶 × 逐模型 TTFT/TPOT 分位，只读 display filter）。 */

  /* v6.0: 模型折线 5 色分类调色板（CVD 安全，固定顺序、绝不循环；主题各自校验通过）。
   * 同屏上限 = 本数组长度（5，CVD 驱动）；改动须重跑 dataviz validate_palette.js 保持 ALL PASS。
   * 模型 1..5 按请求数降序分色，与 chip 同序 → 同色；dark/light 各自一组（house 惯例）。 */
  var _MODEL_COLORS = {
    dark:  ['#3a86e0', '#b57a14', '#12a594', '#8b5cf6', '#e0574a'],
    light: ['#1f6fd6', '#b45309', '#0e8f7f', '#7c3aed', '#cf4636'],
  };

  var _latWin = '24h';
  var _latQ = 'p50';               // 'p50' | 'p50p95'
  var _latSel = null;              // 选中模型名数组（null=默认取前 4）
  var _latCache = {};              // window -> { data, at }
  var _latFetchInflight = {};
  var _LAT_TTL = { '1h': 30000, '24h': 60000, '7d': 60000 };   // 与后端 _LAT_CACHE_TTL 逐档对齐

  function _modelColors() {
    /* currentTheme 在 IFCharts 命名空间（charts.js 导出）；monitor.js 是 strict IIFE，
       裸调用会 ReferenceError。守卫模式同 L875 onThemeChange 用法。 */
    var th = (IFCharts && typeof IFCharts.currentTheme === 'function')
      ? IFCharts.currentTheme() : 'dark';
    return _MODEL_COLORS[th] || _MODEL_COLORS.dark;
  }

  function getLatSeries(win) {
    var c = _latCache[win];
    var now = Date.now();
    if (c && now - c.at < (_LAT_TTL[win] || 60000)) return c.data;
    if (!_latFetchInflight[win]) {
      _latFetchInflight[win] = true;
      fetch('/api/latency?window=' + win, { cache: 'no-store' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          _latCache[win] = { data: data || {}, at: Date.now() };
          if (isMonitorActive()) renderLatencyCards();
        })
        .catch(function (e) { console.warn('[monitor] /api/latency fetch failed:', e); })
        .then(function () { _latFetchInflight[win] = false; });
    }
    return c ? c.data : {};
  }

  function _latAvailable(seriesObj) { return Object.keys(seriesObj || {}); }  // 请求数降序

  // chip 列表 = 请求数前 5（上限 = 调色板长度，CVD 驱动）；默认选中前 4
  function _latChipModels(seriesObj) {
    return _latAvailable(seriesObj).slice(0, _modelColors().length);
  }

  function _latSelected(seriesObj) {
    var chips = _latChipModels(seriesObj);
    // 仅在有模型数据时才固化默认选中：冷启动首渲染（数据未到）不得把 _latSel
    // 从 null 固化为 []（[] 为 truthy → 数据到达后默认前 4 不再触发）。
    // 用户主动全取消（_latSel=[]）不受影响：有 chips 且 _latSel 非 null 时不重设。
    if (!_latSel && chips.length) _latSel = chips.slice(0, Math.min(4, chips.length));
    var sel = chips.filter(function (n) { return _latSel.indexOf(n) >= 0; });
    return sel.slice(0, _modelColors().length);   // 安全网：不会超调色板长度
  }

  function _latColorOf(name, seriesObj) {
    var i = _latAvailable(seriesObj).indexOf(name);
    return i >= 0 ? _modelColors()[i] : '#888';   // 按 rank 定色：chip 与图同色，绝不循环
  }

  function _latBucketLabel(win) { return win === '1h' ? '5min' : (win === '7d' ? '6h' : '1h'); }

  function _lowPt(v, n, color) {
    // 低置信（0<n<30）→ 半透明小圆点；n=0/无值 → null（断线）。
    // house 规则 series 级 symbol:'none'（无逐点标记）下，仅 itemStyle.opacity
    // 的 data item 不渲染任何东西——必须显式 symbol:'circle' 才可见（fix round 1）。
    if (v == null) return null;
    var low = n > 0 && n < 30;
    return low
      ? { value: v, symbol: 'circle', symbolSize: 6,
          itemStyle: { color: color, opacity: 0.45 } }
      : v;
  }

  function renderLatChips(seriesObj) {
    var el = $('monLatChips'); if (!el) return;
    var chips = _latChipModels(seriesObj);
    if (!chips.length) { el.innerHTML = '<span class="muted" style="font-size:11px">暂无模型</span>'; return; }
    var sel = _latSelected(seriesObj);
    var html = '';
    chips.forEach(function (name) {
      var on = sel.indexOf(name) >= 0;
      html += '<button type="button" class="mon-lat-chip' + (on ? ' on' : '') +
        '" data-model="' + escHtml(name) + '" style="--mc:' + _latColorOf(name, seriesObj) + '">' +
        '<span class="dot"></span>' + escHtml(shortName(name)) + '</button>';
    });
    el.innerHTML = html;
  }

  function _latTrendTooltip(prefix) {
    return function (params) {
      if (!params || !params.length) return '';
      var bucket = params[0].axisValue;
      var rows = '';
      params.forEach(function (p) {
        if (p.value == null || (typeof p.value === 'object' && p.value == null)) return;
        var v = (p.value && typeof p.value === 'object') ? p.value.value : p.value;
        rows += '<div>' + escHtml(p.seriesName) + '：' + v +
          (prefix === 'ttft' ? ' ms' : ' ms/token') + '</div>';
      });
      var unit = prefix === 'ttft' ? ' ms' : ' ms/token';
      return '<div><b>' + bucket + '</b></div>' + rows;
    };
  }

  function renderLatCard(metric) {
    var isTtft = metric === 'ttft';
    var prefix = isTtft ? 'ttft' : 'tpot';
    var unit = isTtft ? 'ms' : 'ms/token';
    if (!_charts[metric]) return;
    var data = getLatSeries(_latWin);
    var seriesObj = (data && data.series) || {};
    var buckets = (data && data.buckets) || [];
    var sel = _latSelected(seriesObj);

    var series = [];
    var legendData = [];
    sel.forEach(function (name) {
      var s = seriesObj[name] || {};
      var color = _latColorOf(name, seriesObj);   // rank→色，与 chip 一致
      var n = s[prefix + '_n'] || [];
      var p50 = (s[prefix + '_p50'] || []).map(function (v, bi) { return _lowPt(v, n[bi], color); });
      series.push({
        name: name, type: 'line', data: p50, connectNulls: false,
        lineStyle: { color: color, width: 2 },
        itemStyle: { color: color },
      });
      legendData.push(name);
      if (_latQ === 'p50p95') {
        var p95 = (s[prefix + '_p95'] || []).map(function (v, bi) { return _lowPt(v, n[bi], color); });
        series.push({
          name: name + ' P95', type: 'line', data: p95, connectNulls: false,
          lineStyle: { color: color, width: 1, type: 'dashed' },
          itemStyle: { color: color },
        });
      }
    });

    var hasData = sel.length > 0 && buckets.length > 0;
    showEmpty(isTtft ? 'monTtftEmpty' : 'monTpotEmpty', !hasData);
    IFCharts.update(_charts[metric], {
      xAxis: { type: 'category', data: buckets, boundaryGap: false,
               axisLabel: { fontSize: 11, interval: 'auto' } },
      yAxis: { type: 'value', axisLabel: { formatter: '{value} ' + unit } },
      legend: { show: series.length >= 1, data: legendData, textStyle: { fontSize: 11 } },
      tooltip: { trigger: 'axis', formatter: _latTrendTooltip(prefix) },
      series: series,
    }, { replaceSeries: true });   // series 数随 chip/P95 收缩 → 整替，防幽灵 series 残留
  }

  function renderLatencyCards() {
    ensureCharts();
    var data = getLatSeries(_latWin);
    var seriesObj = (data && data.series) || {};
    renderLatChips(seriesObj);
    var bl = _latBucketLabel(_latWin);
    var sub1 = $('monTtftSub'), sub2 = $('monTpotSub'), sub0 = $('monLatSub');
    if (sub1) sub1.textContent = 'ms · ' + _latWin + ' · 每 ' + bl;
    if (sub2) sub2.textContent = 'ms/token · ' + _latWin + ' · 每 ' + bl;
    if (sub0) sub0.textContent = '时间分桶 · 桶内 ' + (_latQ === 'p50p95' ? 'P50/P95' : 'P50');
    renderLatCard('ttft');
    renderLatCard('tpot');
  }

  /* ── 4. 六联 KPI（2 行 × 3 列）──
   * GET /api/engine_metrics?model=<active> → kv_cache_usage_perc /
   * running_batch(live 在途并发, 0..max_batch) / seq_length /
   * tpot_seconds.mean(→ms) / ttft_seconds.mean(→s) / throughput */
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
    // 休眠模型或无指标数据（running_batch 存在 = 引擎存活，即便 0 也不算休眠）
    if (data.sleep_state === 0 && data.kv_cache_usage_perc == null &&
        data.seq_length == null && data.throughput == null &&
        data.ttft_seconds == null && data.tpot_seconds == null &&
        data.running_batch == null) {
      el.innerHTML = '<div class="if-empty">模型休眠中，无实时指标 — 到推理 TAB 唤醒</div>';
      return;
    }

    var kv = data.kv_cache_usage_perc;
    var batch = data.running_batch;        // live 在途并发（最近 20 条非零采样平均，跳过 idle 0）
    var maxBatch = data.max_batch;         // 引擎上限 max_concurrency / max_num_seqs
    var seq = data.seq_length;
    var tpot = data.tpot_seconds;
    var ttft = data.ttft_seconds;
    var thr = data.throughput;

    // 单位统一：TPOT → ms（2 位小数）；TTFT → 秒 s（2 位小数）。
    // Batch Size = 在途并发请求数的「最近 20 条非零采样平均」（live 值，跳过 idle 0 采样，
    // 避免单点跌 0 / 被零值拉低；上限 max_batch），非累计完成数。
    el.innerHTML =
      '<div class="mon-kpi-grid">' +
        kpiTile('KV Cache', kv != null ? Number(kv).toFixed(1) + '%' : '—',
          'KV 缓存占用率（来自引擎 /metrics）') +
        kpiTile('Batch Size',
          batch != null ? (maxBatch ? batch + ' / ' + maxBatch : String(batch)) : '—',
          '在途并发请求数（live，最近 20 条非零采样平均，跳过 idle 0 采样；上限 max_concurrency）') +
        kpiTile('Seq Length', seq != null ? UI.fmtNum(seq) : '—',
          '平均请求序列长度（prompt + generation tokens）') +
        kpiTile('TPOT', tpot && tpot.mean != null ? (tpot.mean * 1000).toFixed(2) + 'ms' : '—',
          'Time Per Output Token — 每输出 token 生成耗时（ms，保留 2 位）') +
        kpiTile('TTFT', ttft && ttft.mean != null ? ttft.mean.toFixed(2) + 's' : '—',
          'Time To First Token — 首 token 延迟（秒，保留 2 位）') +
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
      // 缓存命中率（Cache Hit Rate）= tokens_in_cached / tokens_in；
      // 双协议统一口径（OpenAI/Anthropic 均由 normalize_usage 归一化）
      var cacheRate = '—';
      if (l.tokens_in > 0) {
        var _c = Math.min(l.tokens_in_cached || 0, l.tokens_in);
        cacheRate = (_c / l.tokens_in * 100).toFixed(1) + '%';
      }
      var ttft = l.ttft_ms != null ? l.ttft_ms.toFixed(0) + 'ms' : '—';
      var dur = l.duration_ms != null ? l.duration_ms.toFixed(0) + 'ms' : '—';
      rows += '<tr>' +
        '<td class="mono">' + escHtml(ts) + '</td>' +
        '<td>' + escHtml(shortName(l.model)) + '</td>' +
        '<td><span class="badge ' + stCls + '">' + escHtml(l.status) + '</span></td>' +
        '<td class="mono num">' + escHtml(tokIn + ' / ' + tokOut) + '</td>' +
        '<td class="mono num">' + escHtml(cacheRate) + '</td>' +
        '<td class="mono num">' + escHtml(ttft) + '</td>' +
        '<td class="mono num">' + escHtml(dur) + '</td>' +
      '</tr>';
    }
    el.innerHTML =
      '<div class="cp-table-wrap">' +
      '<table class="if-table mon-tbl">' +
        '<thead><tr><th>时间</th><th>模型</th><th>状态</th>' +
        '<th>Tokens in/out</th>' +
        '<th title="Cache Hit Rate（缓存命中率）= tokens_in_cached / tokens_in；OpenAI 系取 prompt_tokens_details.cached_tokens，Anthropic 系取 cache_read_input_tokens，口径统一">缓存命中率</th>' +
        '<th>TTFT</th><th>耗时</th></tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>' +
      '</div>';
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
      '<div class="cp-table-wrap">' +
      '<table class="if-table mon-tbl">' +
        '<thead><tr><th>时间</th><th>From</th><th>To</th><th>耗时</th><th>状态</th></tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>' +
      '</div>';
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
    renderLatencyCards();
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
      if (btn && tabEl.contains(btn)) {
        var seg = btn.closest('.mon-seg');
        if (seg) {
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
          } else if (segType === 'latwin') {
            var latWin = btn.getAttribute('data-win');
            if (latWin && latWin !== _latWin) {
              _latWin = latWin;
              _latSel = null;   // 切窗口后重置默认（按新窗口请求数序取前 4）
              renderLatencyCards();
            }
          } else if (segType === 'latq') {
            var q = btn.getAttribute('data-q');
            if (q && q !== _latQ) {
              _latQ = q;
              renderLatencyCards();
            }
          }
        }
        return;
      }
      // chip（不在 .mon-seg 内）
      var chip = ev.target.closest('.mon-lat-chip');
      if (chip && tabEl.contains(chip)) {
        var m = chip.getAttribute('data-model');
        var latData = getLatSeries(_latWin) || {};
        if (!_latSel) _latSel = _latSelected((latData.series || {}));
        var i = _latSel.indexOf(m);
        if (i >= 0) _latSel.splice(i, 1); else _latSel.push(m);
        // 无超限分支：chip 已封顶 top 5，选中 ≤ chip 数 = 调色板长度
        renderLatencyCards();
        return;
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
        renderLatencyCards();
      }
    });
  }
})();
