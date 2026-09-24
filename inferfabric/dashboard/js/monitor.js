/* InferFabric Console — Monitor tab (v2, Task 5)
 * 纯遥测、只读、零操作（spec §4.3）。
 *   - 5 ECharts: 功耗/电费单图双轴（v6.2 取代 GPU vram+util 时间曲线——实时值已在
 *     顶部 GPU KPI 卡；柱=平均功耗 W 左轴 + 折线=累计电量/电费 度=元 右轴：
 *     流速↔存量因果对，charts.js 的 dualAxis 显式放行，见 _applyRules 注释）/
 *     Token prompt+completion 堆叠条 / TTFT/TPOT 双卡趋势折线
 *   - 6 KPI（2 行 × 3 列）: KV Cache / Batch Size / Seq Length / TPOT(ms) / TTFT(s) / Throughput
 *     （GET /api/engine_metrics）
 *   - 2 表: 请求日志 + 切换历史（13px 紧凑）
 *   - 1 卡: 费用概览
 *
 * 数据源（全部 GET，只读）：
 *   - store /api/snapshot → metrics_24h request_log history gpu gpu_util active_services
 *     （token_stats 30d 属 overview.js 7 天趋势，Monitor Token 卡四档改走端点）
 *   - GET /api/token-curve?granularity=minute|hour|day|week → Token 用量四档
 *     （服务端分桶 + limit=100000；label=分桶单位：分钟=12×5min/小时=24×1h/
 *      天=30×1d/周=13×7d；local/cloud 双 scope；月整体弃用）
 *   - GET /api/engine_metrics?model=<active> → 6 KPI 原始指标
 *   - GET /api/power?gran=hour|day|week → 功耗/电费分桶（5min TTL；服务端 60s
 *     采样落 SQLite，页面关着历史也连续；小时=近24h/天=近30天/周=近~90天）
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
  var _pgran = 'hour';           // 功耗/电费图表粒度 display filter（小时/天/周）
  var _tokenGran = 'hour';       // Token 图表粒度 display filter（分钟/小时/天/周）
  var _charts = { power: null, tokenLocal: null, tokenCloud: null, ttft: null, tpot: null };
  var _chartsInit = false;

  // 功耗/电费卡独立 TTL 缓存（5min）——历史功耗无需秒级刷新；不随 3s snapshot 重绘。
  // 数据源 /api/power（服务端 60s 采样 + v007 表 + 5min 后端 TTL），与延迟卡同构。
  var _pgranCache = {};
  var _pgranInflight = {};
  var _POWER_TTL = 300000;       // 5min——与后端 _power_series_cache 对齐
  var _pgranRendered = null;     // 已完整渲染（热缓存 + 同档 → 3s 轮询下跳过重绘）

  // 引擎指标节流（避免每 3s 轮询都打 /api/engine_metrics）
  var _engineCache = null;
  var _engineModel = null;
  var _lastEngineFetch = 0;
  var ENGINE_TTL = 15000;        // 15s

  // Token 卡四档统一节流 + 数据源（单位语义统一 v6.3）：
  // 四档都走 GET /api/token-curve?granularity=（服务端分桶 + limit=100000；
  // 不再用 token_stats 的 day/月 —— token_stats 30d 留存且随 snapshot 3s 轮询，
  // 周档只有端点有 90d 数据）。每档独立缓存：_tokenCurveCache[gran] = {data, at}，
  // data = 端点响应 {local, cloud}，local[i]/cloud[i] = idx i（i=0 最旧 → n-1 最新），
  // 含 prompt/completion/cached 拆分。
  var _tokenCurveCache = {};         // gran -> {data, at}
  var TOKEN_CURVE_TTL = 15000;       // 15s — 同 ENGINE_TTL

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
      _charts.power = IFCharts.create('monPowerChart');
      _charts.tokenLocal = IFCharts.create('monTokenLocalChart');
      _charts.tokenCloud = IFCharts.create('monTokenCloudChart');
      _charts.ttft = IFCharts.create('monTtftChart');
      _charts.tpot = IFCharts.create('monTpotChart');
    }
    // null → 容器显示 empty state（IFCharts 已 log warning）
    if (!_charts.power) {
      var p = $('monPowerChart');
      if (p) p.innerHTML = '<div class="if-empty">图表库不可用</div>';
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

  /* ── 1. 功耗 / 电费（单图双轴，v6.2 取代 GPU 显存/利用率曲线）──
   * 口径：GPU 板卡功耗（nvidia-smi power.draw，含 idle），¥1/度。
   * 粒度 hour/day/week = display filter（服务端分桶，GET /api/power；月整体弃用）。
   *   - 柱（左轴 W）  = 每桶平均功耗——看"哪个时段烧得凶"
   *   - 折线（右轴 度=元） = 窗口起点累计电量/电费——看"一共烧了多少、花了多少"
   * 双轴合法性：功耗↔累计电量是 流速↔存量 因果对（累计=功率对时间积分），
   * 非 TTFT/TPOT 那种无关量纲对比；故显式 {dualAxis:true} 放行（charts.js 唯一受权例外）。
   * 刷新：5min TTL 独立拉取 + 完成回调重绘；不随 3s snapshot 重绘（热缓存+同档跳过）。 */
  function _pgranWinText(gran) {
    if (gran === 'day') return '近 30 天';
    if (gran === 'week') return '近 90 天';
    return '近 24h';
  }

  /* 右轴（度）刻度标签：总量小（<1 度）时 2 位小数不被 toFixed(1) 压成 0.0；
   * 步长恒为 {1,2,5}×10^k（见 _niceCeil），去掉尾零 —— 0.20 度 → 0.2 度。 */
  function _pgranKwhFmt(v) {
    var s = (Math.abs(v) >= 1 ? Number(v).toFixed(1) : Number(v).toFixed(2));
    return s.replace(/0+$/, '').replace(/\.$/, '') + ' 度';
  }

  /* nice 上取整：返回最小的 n×步长 ≥ v，步长 ∈ {1,2,5}×10^k。
   * 右轴（度）用它定 max —— max/n 是干净步长，n 等分下每格标签
   * 总量大时是整数（37 度 → max 60 → 0,10,…,60），
   * 总量 <1 度时是 0.1/0.2/0.5 档干净小数（0.64 度 → max 1.2 → 0,0.2,…,1.2）。 */
  function _niceCeil(v, n) {
    if (!(v > 0)) v = 1;
    var raw = v / n;
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var norm = raw / mag;           // [1, 10)
    var step;
    if (norm <= 1) step = 1;
    else if (norm <= 2) step = 2;
    else if (norm <= 5) step = 5;
    else step = 10;
    // 拍掉浮点噪声（0.6442/6→step 0.2 → n*step*mag=1.2000000000000002）：
    // 若带回 1.2000000000000002，echarts 刻度数 = ceil(max/interval)
    // = ceil(6.000000000000001)=7，与左轴 6 等分错位 → 网格对不齐。
    return +((n * step * mag).toFixed(12));
  }

  function _pgranLabels(buckets, gran) {
    return buckets.map(function (b) {
      var d = new Date(b.t * 1000);   // b.t = 桶起点 epoch 秒（服务端本地时区对齐）
      if (gran === 'day' || gran === 'week') return (d.getMonth() + 1) + '-' + d.getDate();
      return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    });
  }

  function getPgranSeries(gran) {
    var c = _pgranCache[gran];
    var now = Date.now();
    if (c && now - c.at < _POWER_TTL) return c.data;
    if (!_pgranInflight[gran]) {
      _pgranInflight[gran] = true;
      fetch('/api/power?gran=' + gran, { cache: 'no-store' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          _pgranCache[gran] = { data: data || {}, at: Date.now() };
          if (isMonitorActive()) drawPower();
        })
        .catch(function (e) { console.warn('[monitor] /api/power fetch failed:', e); })
        .then(function () { _pgranInflight[gran] = false; });
    }
    return c ? c.data : {};
  }

  function _pgranTooltip(params) {
    if (!params || !params.length) return '';
    var label = params[0].axisValue;
    var rows = '';
    for (var i = 0; i < params.length; i++) {
      var p = params[i];
      if (p.seriesName === '平均功耗') {
        rows += '<div>' + p.seriesName + '：' +
          (p.value == null ? '—' : Math.round(p.value) + ' W') + '</div>';
      } else if (p.seriesName === '累计电量') {
        // 数据为对象式点位 {value, symbol:'circle'}——逐桶打点后须解包
        var vRaw = p.value;
        var v = (vRaw && typeof vRaw === 'object') ? vRaw.value : vRaw;
        rows += '<div>' + p.seriesName + '：' +
          (v == null ? '—' : Number(v).toFixed(2) + ' 度 · ¥' + Number(v).toFixed(2)) + '</div>';
      }
    }
    return '<div><b>' + label + '</b></div>' + rows;
  }

  function drawPower() {
    ensureCharts();
    var data = getPgranSeries(_pgran);
    var buckets = (data && data.buckets) || [];
    var totals = (data && data.totals) || {};
    var available = !!(data && data.available);

    // hero 行：窗口累计电费（大字）+ 累计度数 · 平均功率 · 窗口
    var heroCost = $('monPowerCost');
    var heroMeta = $('monPowerMeta');
    if (heroCost) {
      heroCost.textContent = '¥' + (totals.yuan || 0).toFixed(2);
    }
    if (heroMeta) {
      heroMeta.textContent = '累计 ' + (totals.kwh || 0).toFixed(1) + ' 度 · 平均 ' +
        (totals.avg_w != null ? Math.round(totals.avg_w) + ' W' : '—') +
        ' · ' + _pgranWinText(_pgran);
    }

    if (!_charts.power) return;
    showEmpty('monPowerEmpty', !available || !buckets.length);
    if (!available || !buckets.length) {
      // 空态也须给出完整双轴骨架（charts.js 双轴为显式受权例外，见 _applyRules
      // opts.dualAxis；缺 axis 数组 + dualAxis 时 yAxisIndex:1 引用不存在轴，
      // echarts 首渲染即抛 cartesian2d getInitialData 异常）
      IFCharts.update(_charts.power, {
        xAxis: { data: [], boundaryGap: true },
        // 双轴同构骨架：左 W 固定 0–600（卡 TDP 封顶，不会突破，无需动态取整）；
        // 右 度 空态 _niceCeil(0,6)=1.2 → 0,0.2,…,1.2。两轴同 splitNumber:6 + min:0
        // → 6 等分像素位置重合，右轴只印标签不画网格线，一套尺度线（左轴的）。
        yAxis: [
          { name: 'W', min: 0, max: 600, splitNumber: 6, position: 'left',
            axisLabel: { formatter: function (v) { return v + ' W'; } } },
          { name: '度', min: 0, max: _niceCeil(0, 6), splitNumber: 6,
            splitLine: { show: false }, position: 'right',
            axisLabel: { formatter: _pgranKwhFmt } },
        ],
        tooltip: { trigger: 'axis', formatter: _pgranTooltip },
        legend: { data: ['平均功耗', '累计电量'] },
        series: [
          { type: 'bar', name: '平均功耗', yAxisIndex: 0, data: [], z: 2 },
          { type: 'line', name: '累计电量', yAxisIndex: 1, data: [], z: 3 },
        ],
      }, { dualAxis: true });
      return;
    }

    var xs = _pgranLabels(buckets, _pgran);
    var avg = buckets.map(function (b) { return b.avg_w == null ? null : b.avg_w; });
    // 累计线：全部桶逐桶打点（方案 B）——前导空桶 cum=0 也带点、数据桶带真值，
    // 线从窗口起点 0 连续到末端总量；对象式点位在 house symbol:'none' 下才可见。
    var cum = buckets.map(function (b) {
      return { value: b.cum_kwh, symbol: 'circle', symbolSize: 5 };
    });
    // 左轴 W 固定 0–600（卡 TDP 封顶，不会突破）；右轴 度 max = _niceCeil(累计量, 6)
    // —— nice 上取整到 6 等分干净步长，max/6 恒为 {1,2,5}×10^k：
    //   总量大 → 整数刻度（37 度 → max 60 → 0,10,…,60），总量 <1 度 → 0.1/0.2/0.5 档。
    // 两轴同 splitNumber:6 + min:0 → 6 等分像素位置重合，右轴只印标签（splitLine 隐藏），
    // 只有左轴一套尺度线——「坐标整数」「max 随实际调节」「不能有两套尺度线」三点齐。
    var maxKwh = _niceCeil((totals.kwh || 0) || 1, 6);

    IFCharts.update(_charts.power, {
      xAxis: { data: xs, boundaryGap: true },
      yAxis: [
        { name: 'W', min: 0, max: 600, splitNumber: 6, position: 'left',
          axisLabel: { formatter: function (v) { return v + ' W'; } } },
        { name: '度', min: 0, max: maxKwh, splitNumber: 6,
          splitLine: { show: false }, position: 'right',
          axisLabel: { formatter: _pgranKwhFmt } },
      ],
      tooltip: { trigger: 'axis', formatter: _pgranTooltip },
      legend: { data: ['平均功耗', '累计电量'] },
      series: [
        { type: 'bar', name: '平均功耗', yAxisIndex: 0, data: avg, barWidth: '55%', z: 2 },
        { type: 'line', name: '累计电量', yAxisIndex: 1, data: cum, z: 3,
          areaStyle: { opacity: 0.08 } },
      ],
    }, { dualAxis: true });
  }

  function renderPowerCard() {
    // 热缓存 + 本档已渲染 → 数据没变，跳过重绘（3s 轮询不闪图）；
    // 只读路径（getPgranSeries 内部 TTL 过期 → fetch → 落地 drawPower()）。
    ensureCharts();
    var c = _pgranCache[_pgran];
    if (c && (Date.now() - c.at) < _POWER_TTL && _pgranRendered === _pgran) return;
    drawPower();
    _pgranRendered = _pgran;
  }

  /* ── 2. Token 用量：prompt/completion 堆叠条 ──
   * 2 系列（Prompt + Completion），单 y 轴。
   * 粒度 minute/hour/day/week = display filter，四档统一走端点：
   *   GET /api/token-curve?granularity=minute|hour|day|week
   *（服务端分桶：minute=12×5min / hour=24×1h / day=30×1d / week=13×1周；
   *  token_stats 的 day/月 已弃用 —— 周档只有端点有 90d 数据）。
   * 响应 {local:[...], cloud:[...]} 双 scope，各桶含 prompt/completion/cached 拆分。 */
  // 每档桶数 n 与桶宽（ms），与服务端 spec 字典一一对应
  var _TOKEN_N = { minute: 12, hour: 24, day: 30, week: 13 };
  var _TOKEN_W = { minute: 5 * 60000, hour: 3600000, day: 86400000, week: 7 * 86400000 };
  var _tokenCurveInflight = {};   // gran -> true（防并发重复 fetch）

  function buildTokenData(gran, scope) {
    var now = Date.now();

    // 同步返回缓存（命中 TTL）；否则启动异步 fetch 并返回上次缓存或全零（不阻塞渲染）。
    var hit = _tokenCurveCache[gran];
    if (hit && (now - hit.at) < TOKEN_CURVE_TTL) {
      return _tokenFromBuckets(gran, hit.data[scope] || [], now);
    }
    if (!_tokenCurveInflight[gran]) {
      _tokenCurveInflight[gran] = true;
      fetch('/api/token-curve?granularity=' + gran, { cache: 'no-store' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          // 保留 local + cloud 两个 scope（供本地/云端两张图各自取用）
          _tokenCurveCache[gran] = { data: data || {}, at: now };
        })
        .catch(function (e) {
          console.warn('[monitor] token-curve(' + gran + ') fetch failed:', e);
          // 失败不清缓存（保留下次可用旧值）；无缓存则置空
        })
        .then(function () {
          _tokenCurveInflight[gran] = false;
          if (isMonitorActive()) renderTokenChart();
        });
    }
    return _tokenFromBuckets(gran, (hit && hit.data[scope]) || [], now);
  }

  // 把 token-curve 的 scope 桶数组（idx 0=最旧→n-1=最新）转成图表数据。
  // 无缓存时返回 n 个零桶（空图 + empty state）。
  function _tokenFromBuckets(gran, buckets, now) {
    var n = _TOKEN_N[gran] || 24;
    var w = _TOKEN_W[gran] || 3600000;
    var xs = [], prompt = [], comp = [];
    for (var k = 0; k < n; k++) {
      var end = now - (n - 1 - k) * w;   // 桶结束时刻（最旧桶 = now-(n-1)w，最新桶 = now）
      xs.push(_tokenGranLabel(gran, end));
      var b = (buckets && buckets[k]) || {};
      prompt.push(b.prompt || 0);
      comp.push(b.completion || 0);
    }
    return { xs: xs, prompt: prompt, completion: comp };
  }

  // 桶标签：minute → HH:mm（桶结束时刻）；hour → HH:00；day/week → MM-DD
  function _tokenGranLabel(gran, endDate) {
    var d = new Date(endDate);
    if (gran === 'minute') return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    if (gran === 'hour') return String(d.getHours()).padStart(2, '0') + ':00';
    return (d.getMonth() + 1) + '-' + d.getDate();
  }

  /* 缓存命中率（Cache Hit Rate）= 缓存命中 prompt tokens / 总 prompt tokens。
   * 双协议统一口径：OpenAI 系取 prompt_tokens_details.cached_tokens（计入
   * prompt_tokens）；Anthropic 系取 cache_read_input_tokens（creation 计入
   * 总量、不计命中）。窗口 = 当前粒度的全部桶（端点响应 cached/prompt 汇总；
   * 不再用 token_stats 的 30d）。 */
  function _scopeCacheHitRate(scope) {
    var hit = _tokenCurveCache[_tokenGran];
    var buckets = (hit && hit.data[scope]) || [];
    var p = 0, c = 0;
    for (var k = 0; k < buckets.length; k++) {
      var b = buckets[k] || {};
      p += b.prompt || 0;
      c += b.cached || 0;
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
   * 共享控制条：窗口 分钟/小时/天（latwin，minute/hour/day，数据仅 30d 不做周）
   * + 分位 P50/P50+P95（latq）+ 模型 chip 选择器（≤5 个在用模型，默认全选中；
   * 颜色按 rank 定，chip 与图同色）。
   * 数据源：GET /api/latency?window=（时间分桶 × 逐模型 TTFT/TPOT 分位，只读 display filter）。 */

  /* v6.0: 模型折线 5 色分类调色板（CVD 安全，固定顺序、绝不循环；主题各自校验通过）。
   * 同屏上限 = 本数组长度（5，CVD 驱动）；改动须重跑 dataviz validate_palette.js 保持 ALL PASS。
   * 模型 1..5 按请求数降序分色，与 chip 同序 → 同色；dark/light 各自一组（house 惯例）。 */
  var _MODEL_COLORS = {
    dark:  ['#3a86e0', '#b57a14', '#12a594', '#8b5cf6', '#e0574a'],
    light: ['#1f6fd6', '#b45309', '#0e8f7f', '#7c3aed', '#cf4636'],
  };

  var _latWin = 'hour';            // minute | hour | day（数据仅 30d，无周档）
  var _latQ = 'p50';               // 'p50' | 'p50p95'
  var _latSel = null;              // 选中模型名数组（null=默认全部在用模型）
  var _latCache = {};              // window -> { data, at }
  var _latFetchInflight = {};
  var _LAT_TTL = { minute: 300000, hour: 300000, day: 300000 };   // 5min，与后端 _LAT_CACHE_TTL 逐档对齐
  var _latWinText = { minute: '近 60min', hour: '近 24h', day: '近 30 天' };   // 副标题窗口文案

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

  // chip 列表 = 全部在用模型（后端已按请求数截断 ≤5 = 调色板长度，CVD 驱动）；默认全选中
  function _latChipModels(seriesObj) {
    return _latAvailable(seriesObj).slice(0, _modelColors().length);
  }

  function _latSelected(seriesObj) {
    var chips = _latChipModels(seriesObj);
    // 仅在有模型数据时才固化默认选中：冷启动首渲染（数据未到）不得把 _latSel
    // 从 null 固化为 []（[] 为 truthy → 数据到达后默认全选中不再触发）。
    // 用户主动全取消（_latSel=[]）不受影响：有 chips 且 _latSel 非 null 时不重设。
    if (!_latSel && chips.length) _latSel = chips.slice();
    var sel = chips.filter(function (n) { return _latSel.indexOf(n) >= 0; });
    return sel.slice(0, _modelColors().length);   // 安全网：不会超调色板长度
  }

  function _latColorOf(name, seriesObj) {
    var i = _latAvailable(seriesObj).indexOf(name);
    return i >= 0 ? _modelColors()[i] : '#888';   // 按 rank 定色：chip 与图同色，绝不循环
  }

  function _latBucketLabel(win) { return ({ minute: '5min', hour: '1h', day: '1d' })[win] || '1h'; }

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
        // tooltip 值归一：TTFT 秒（2 位小数）、TPOT ms/token（2 位小数）
        var txt = prefix === 'ttft'
          ? (v / 1000).toFixed(2) + ' s'
          : Number(v).toFixed(2) + ' ms/tok';
        rows += '<div>' + escHtml(p.seriesName) + '：' + txt + '</div>';
      });
      return '<div><b>' + bucket + '</b></div>' + rows;
    };
  }

  function renderLatCard(metric) {
    var isTtft = metric === 'ttft';
    var prefix = isTtft ? 'ttft' : 'tpot';
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
        name: name, type: 'line', data: p50, connectNulls: true,   // 中间空桶桥接（视觉平滑，不造数据点）；首尾空不延伸
        lineStyle: { color: color, width: 2 },
        itemStyle: { color: color },
      });
      legendData.push(name);
      if (_latQ === 'p50p95') {
        var p95 = (s[prefix + '_p95'] || []).map(function (v, bi) { return _lowPt(v, n[bi], color); });
        series.push({
          name: name + ' P95', type: 'line', data: p95, connectNulls: true,
          lineStyle: { color: color, width: 1, type: 'dashed' },
          itemStyle: { color: color },
        });
      }
    });

    var hasData = sel.length > 0 && buckets.length > 0;
    showEmpty(isTtft ? 'monTtftEmpty' : 'monTpotEmpty', !hasData);
    // y 轴刻度简化：TTFT 量级 ~1k-16k ms → 归一秒（/1000，1 位小数，如 3.0 s）；
    // TPOT 量级 0-18 ms/token、间隔整数 → 整数刻度（0/3/6/9…，不留小数）。
    var yFormatter = isTtft
      ? (function (v) { return (v / 1000).toFixed(1) + ' s'; })
      : (function (v) { return Math.round(v) + ' ms/tok'; });
    IFCharts.update(_charts[metric], {
      xAxis: { type: 'category', data: buckets, boundaryGap: false,
               axisLabel: { fontSize: 11, interval: 'auto' } },
      yAxis: { type: 'value', axisLabel: { formatter: yFormatter, fontSize: 11 } },
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
    var winTxt = _latWinText[_latWin] || '近 24h';
    if (sub1) sub1.textContent = 'ms · ' + winTxt + ' · 每 ' + bl;
    if (sub2) sub2.textContent = 'ms/token · ' + winTxt + ' · 每 ' + bl;
    if (sub0) sub0.textContent = '时间分桶 · 桶内 ' + (_latQ === 'p50p95' ? 'P50/P95' : 'P50');
    renderLatCard('ttft');
    renderLatCard('tpot');
  }

  /* ── 4. 九联 KPI（3 行 × 3 列）──
   * 前 6 卡：GET /api/engine_metrics?model=<active> → kv_cache_usage_perc /
   * running_batch(live 在途并发, 0..max_batch) / seq_length /
   * tpot_seconds.mean(→ms) / ttft_seconds.mean(→s) / throughput
   * 后 3 卡（v6.1）：store.get('metrics_24h')（request_log 聚合器，3s 快照轮询
   * 已灌入，无新端点）→ E2E tok/s（P50）/ Req Rate（RPS）/ Avg Out Len */
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

    // v6.1: 新增 3 卡来自 request_log 聚合器（metrics_24h 随 3s 快照灌入 store，
    // 无需新端点）；键 = active 模型名（与 engine_metrics 同源），缺键/无数据 → "—"。
    // 与引擎 6 卡数据源解耦：引擎卡走 _engineCache TTL，新 3 卡随快照刷新。
    var m24 = (store.get('metrics_24h') || {}).models || {};
    var mm = m24[_engineModel] || {};
    var e2e = mm.e2e_tps_p50;      // E2E tok/s（P50）
    var rps = mm.rps;              // 24h 窗口请求速率
    var avgOut = mm.avg_out_len;   // 单请求平均输出长度

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
        kpiTile('E2E tok/s', e2e != null ? Number(e2e).toFixed(1) : '—',
          '端到端速率 P50 = 输出 tokens ÷ 请求总时长（24h 窗口；不依赖 TTFT，流式/非流式通用）') +
        kpiTile('Req Rate', rps != null ? Number(rps).toFixed(4) + ' req/s' : '—',
          '请求速率 RPS = 24h 窗口请求数 ÷ 窗口秒数') +
        kpiTile('Avg Out Len', avgOut != null ? UI.fmtNum(avgOut) + ' tok' : '—',
          '单请求平均输出长度（tokens，24h 窗口）') +
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
      // history.timestamp 来自 SQLite CURRENT_TIMESTAMP 字符串（'YYYY-MM-DD HH:MM:SS'），
      // 非 epoch 数字 → 不能 *1000（NaN→Invalid Date）。兼容两种格式。
      var ts = h.timestamp
        ? new Date(typeof h.timestamp === 'number' ? h.timestamp * 1000 : h.timestamp)
            .toLocaleString('zh-CN',
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
    renderPowerCard();
    renderTokenChart();
    // 延迟趋势卡不随 3s snapshot 重渲染（数据源 /api/latency 有独立 5min TTL 缓存，
    // 数据不变时重画纯属浪费且让图闪）。延迟卡由 getLatSeries 的 TTL 节流：
    // TTL 过期才 fetch → 落地 renderLatencyCards()；命中缓存则不 fetch 不重绘。
    // 交互回调（窗口/chip/分位切换）+ 切回 tab（renderLatencyCards 强制一次）仍即时渲染。
    renderKpis();
    renderLogTable();
    renderHistTable();
    renderCostCard();
  }
  window.tabRenderers['tab-monitor'] = renderMonitor;

  /* ── 订阅：sync_meta → 仅 monitor tab 活跃时刷新 ──
   * 功耗采样在服务端（PowerSampler 60s，页面关着历史也连续），前端零积累——
   * 不再有 GPU ring buffer 采样 push。 */
  store.on('sync_meta', function () {
    if (isMonitorActive()) renderMonitor();
  });

  store.on('tab_active', function (tab) {
    if (tab === 'tab-monitor') {
      // 切到 monitor tab：立即渲染（charts 可能需要 init）
      renderMonitor();
      // 延迟卡 + 功耗卡：TTL 缓存命中即时画 / 过期触发 fetch 落地后重绘
      renderLatencyCards();
      renderPowerCard();
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

          if (segType === 'pgran') {
            var pgran = btn.getAttribute('data-gran');
            if (pgran && pgran !== _pgran) {
              _pgran = pgran;
              _pgranRendered = null;
              renderPowerCard();
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
              _latSel = null;   // 切窗口后重置默认（新窗口全部在用模型）
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
      // 重建后实例已更新，重新渲染所有图表注入数据；功耗卡强制重画
      // （实例已重建，热缓存 guard 的 _pgranRendered 需复位否则被跳过）
      if (isMonitorActive()) {
        _pgranRendered = null;
        renderPowerCard();
        renderTokenChart();
        renderLatencyCards();
      }
    });
  }
})();
