"""Dashboard v2 (InferFabric Console) — 结构断言。增量随任务扩展。"""
from pathlib import Path
from inferfabric.dashboard import get_html, _DASHBOARD_DIR

ROOT = Path(__file__).resolve().parents[3]  # sandbox 根


def _html():
    return get_html()


def test_no_leftover_placeholders():
    html = _html()
    assert "<!-- FRAGMENT:" not in html
    assert "<!-- JS:" not in html
    assert "<!-- CSS:main" not in html


def test_single_head_close_for_token_stats_injection():
    """_serve_dashboard 向 </head> 前注入 __TOKEN_STATS__，必须恰好一个。"""
    assert _html().count("</head>") == 1


def test_dual_theme_tokens():
    html = _html()
    assert '[data-theme="light"]' in html
    for token in ("--accent", "--ink", "--ok", "--warn", "--crit", "--info"):
        assert token in html


def test_shell_structure():
    html = _html()
    for el in ('id="telemetryRail"', 'id="themeToggle"', 'class="if-nav"'):
        assert el in html


def test_overview_structure():
    """总览页 DOM 契约：Task 3 接数据时按这些 id 取节点。"""
    html = _html()
    for el in ('id="ovActiveCard"', 'id="ovActiveBody"', 'id="ovSpark24h"',
               'id="ovAnomTop"', 'id="sysOpsCard"'):
        assert el in html, "missing overview contract id: %s" % el
    # 系统操作按钮 data-action 契约
    for act in ('release', 'reconcile', 'reload', 'reset'):
        assert 'data-action="%s"' % act in html, "missing sys-ops data-action: %s" % act


def test_no_emoji_in_shell():
    import re
    # 顶栏/侧栏区域不得含 emoji 区段（U+1F300-1FAFF, U+2600-27BF）
    m = re.search(r'<header.*?</header>', _html(), re.S)
    assert m and not re.findall(r'[\U0001F300-\U0001FAFF☀-➿]', m.group(0))


def test_icon_symbols_present():
    """新增 SVG symbol id 必须就位（扩集抽样）。"""
    html = _html()
    for sym in ('id="s-play"', 'id="s-stop"', 'id="s-key"', 'id="s-trash"',
                'id="s-search"', 'id="s-shield"', 'id="s-database"'):
        assert sym in html


def test_js_modules_list():
    """__init__.py 必须用显式 JS_MODULES 列表。"""
    import inferfabric.dashboard as dash
    assert getattr(dash, "JS_MODULES", None) == [
        "ui", "store", "charts", "overview", "inference",
        "monitor", "deploy", "cloud", "anomaly",
    ]


def test_legacy_js_deleted():
    """bindings.js / state.js 已删除。"""
    assert not (ROOT / "inferfabric" / "dashboard" / "js" / "bindings.js").exists()
    assert not (ROOT / "inferfabric" / "dashboard" / "js" / "state.js").exists()


def test_new_js_present():
    """ui.js / store.js 已创建。"""
    assert (ROOT / "inferfabric" / "dashboard" / "js" / "ui.js").exists()
    assert (ROOT / "inferfabric" / "dashboard" / "js" / "store.js").exists()


# ── UI.confirm 监听器泄漏回归测试（Node 行为级 harness） ──────────
# 手写 mock DOM：每个 El 记录 addEventListener；holder.innerHTML 被赋值时
# 清空子缓存（模拟子节点销毁）。验证两次连续 confirm 后，仅当前 onOk 触发。
_CONFIRM_LEAK_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

function El() {
  this._html = '';
  this._childCache = {};
  this._listeners = {};
  this.style = {};
  this.textContent = '';
  this.className = '';
  this._attrs = {};
  var self = this;
  Object.defineProperty(this, 'innerHTML', {
    get: function () { return self._html; },
    set: function (v) { self._html = v; self._childCache = {}; }
  });
}
El.prototype.addEventListener = function (type, fn) {
  (this._listeners[type] = this._listeners[type] || []).push(fn);
};
El.prototype.removeEventListener = function (type, fn) {
  var arr = this._listeners[type]; if (!arr) return;
  var i = arr.indexOf(fn); if (i >= 0) arr.splice(i, 1);
};
El.prototype.getAttribute = function (k) { return this._attrs[k] != null ? this._attrs[k] : null; };
El.prototype.setAttribute = function (k, v) { this._attrs[k] = v; };
El.prototype.focus = function () {};
El.prototype.querySelector = function (sel) {
  if (!this._html) return null;
  if (!this._childCache[sel]) this._childCache[sel] = new El();
  return this._childCache[sel];
};
El.prototype.querySelectorAll = function (sel) {
  var el = this.querySelector(sel); return el ? [el] : [];
};
El.prototype.clickListeners = function () { return this._listeners['click'] || []; };

var holder = new El();
var docListeners = {};
var document = {
  getElementById: function (id) { return id === 'confirmModal' ? holder : new El(); },
  addEventListener: function (type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
  removeEventListener: function (type, fn) {
    var arr = docListeners[type]; if (!arr) return;
    var i = arr.indexOf(fn); if (i >= 0) arr.splice(i, 1);
  }
};

global.window = global;
global.document = document;
global.setTimeout = function () {};
global.confirm = function () { return true; };

var code = fs.readFileSync(process.argv[2], 'utf8');
vm.runInThisContext(code);

var onOk1 = 0, onOk2 = 0;

// Cycle 1: open confirm, then close via Escape (must NOT fire onOk)
global.UI.confirm({ title: 't1', body: 'b1', onOk: function () { onOk1++; } });
(docListeners['keydown'] || []).slice().forEach(function (fn) { fn({ key: 'Escape' }); });

// Cycle 2: open confirm (the current one)
global.UI.confirm({ title: 't2', body: 'b2', onOk: function () { onOk2++; } });
var backdrop2 = holder.querySelector('.if-modal-backdrop');

// Simulate an OK click bubbling to wherever the listener lives (holder or backdrop)
var okEvent = { target: { getAttribute: function (k) { return k === 'data-act' ? 'ok' : null; } } };
holder.clickListeners().slice().forEach(function (fn) { fn(okEvent); });
if (backdrop2) backdrop2.clickListeners().slice().forEach(function (fn) { fn(okEvent); });

console.log(JSON.stringify({
  onOk1: onOk1,
  onOk2: onOk2,
  holderClickListeners: holder.clickListeners().length,
  backdropClickListeners: backdrop2 ? backdrop2.clickListeners().length : 0
}));
"""


def test_ui_confirm_no_listener_leak(tmp_path):
    """UI.confirm 不得在持久 #confirmModal holder 上累积 click 监听器（否则
    会重复触发先前已关闭 confirm 的 onOk 闭包）。

    行为级测试：在 node harness 中加载 ui.js + mock DOM，跑两轮 confirm，
    断言仅第二轮 onOk 触发，且 holder 上无残留 click 监听器。"""
    import json
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    ui_js = ROOT / "inferfabric" / "dashboard" / "js" / "ui.js"
    harness = tmp_path / "confirm_leak_harness.js"
    harness.write_text(_CONFIRM_LEAK_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(ui_js)],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, (
        "node harness failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)
    )
    result = json.loads(proc.stdout.strip())
    # 仅当前（第二轮）onOk 触发；第一轮的 onOk 不得因泄漏重复触发
    assert result["onOk1"] == 0, "stale onOk leaked (re-fired): %s" % result
    assert result["onOk2"] == 1, "current onOk did not fire exactly once: %s" % result
    # holder 上不得残留 click 监听器（泄漏的特征）
    assert result["holderClickListeners"] == 0, (
        "click listener leaked onto persistent holder: %s" % result
    )


# ── Task 3: 总览页接真实数据 ──────────────────────────────────────


def test_overview_js_present():
    """overview.js 模块存在且被 get_html() 内联装配（tabRenderers 注册 + doSystemOps）。"""
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "overview.js"
    assert js_path.exists(), "overview.js missing"
    html = _html()
    # overview.js 在 JS_MODULES 中，由 __init__.py 内联进 HTML
    assert "window.tabRenderers['tab-overview'] = renderOverview" in html, (
        "overview.js tab renderer registration not inlined into HTML"
    )
    assert "window.doSystemOps" in html, (
        "window.doSystemOps not inlined into HTML"
    )
    # 遥测带绑定 + 系统操作端点契约
    assert "renderRail" in html
    assert "'/switch'" in html and "'/reset'" in html
    assert "'/reconcile'" in html and "'/reload-config'" in html


def test_overview_no_fake_script():
    """overview.html 不得残留 Task 2 静态原型假数据 <script>（已由 overview.js 接管）。

    断言：fragment 无任何内联 <script>；无 'prototype-only' 注释标记；
    无假值 '24.1'（原型 VRAM 假值）/ MutationObserver 原型代码。
    """
    frag = (_DASHBOARD_DIR / "fragments" / "overview.html").read_text(encoding="utf-8")
    assert "<script" not in frag, "overview.html must not contain an inline <script> block"
    assert "prototype-only" not in frag
    assert "24.1" not in frag
    assert "MutationObserver" not in frag
    # 占位结构保留（overview.js 按 id 填充）
    assert 'id="ovActiveCard"' in frag
    assert 'id="ovActiveBody"' in frag
    assert 'id="ovSpark24h"' in frag


# ── Task 4: ECharts vendor + 主题图表工厂 + CVD 调色板 ────────────────


def test_echarts_vendored():
    """ECharts 5.5.1 vendor 文件存在，并被 __init__.py 内联进 get_html()。

    断言：vendor 文件 > 900KB；get_html() 含 echarts UMD 标识且体积 > 1MB
    （echarts.min.js 本体 ~1.03MB；未内联时 get_html 仅 ~94KB，故 >1MB 即证明已内联）；
    无残留 <!-- JS:echarts --> 占位符。"""
    vendor = ROOT / "inferfabric" / "dashboard" / "vendor" / "echarts.min.js"
    assert vendor.exists(), "echarts.min.js vendor missing"
    assert vendor.stat().st_size > 900_000, "echarts vendor too small (<900KB)"
    html = _html()
    # echarts UMD 包标识（5.5.1）——内联后必然出现在 HTML 中
    assert "echarts" in html
    assert "5.5.1" in html
    # 内联后单文件体积 ~1.12MB（echarts 1.03MB + shell/css/js ~94KB）
    assert len(html) > 1_000_000, "get_html() size %d < 1MB (echarts not inlined?)" % len(html)
    # 占位符已被替换（test_no_leftover_placeholders 也覆盖）
    assert "<!-- JS:echarts -->" not in html


def test_echarts_loads_before_charts_module():
    """echarts 必须在 charts.js 之前内联（charts.js 依赖 window.echarts）。"""
    html = _html()
    i_echarts = html.find("typeof exports")   # echarts UMD 头部特征
    i_ifcharts = html.find("window.IFCharts")
    assert i_echarts > 0 and i_ifcharts > 0, "echarts UMD head or IFCharts not found"
    assert i_echarts < i_ifcharts, "echarts must be inlined before charts.js (IFCharts)"


def test_charts_js_present():
    """charts.js 模块存在并被 get_html() 内联装配（window.IFCharts 工厂）。"""
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "charts.js"
    assert js_path.exists(), "charts.js missing"
    html = _html()
    # IFCharts 工厂契约
    assert "window.IFCharts" in html, "IFCharts factory not inlined"
    for member in ("create:", "update:", "dispose:", "onThemeChange:", "palettes:"):
        assert member in html, "IFCharts member not inlined: %s" % member
    # spec §7 规则编码进工厂
    assert "axisPointer" in html and "type: 'cross'" in html   # crosshair tooltip
    assert "symbol = 'none'" in html                            # 无逐点标记
    assert "lineStyle.width = 2" in html                        # 细线 2px


def test_chart_palette_values():
    """CVD 验证通过的 8 个系列色 hex（task-4-palette.md）必须逐字出现在 get_html()。

    固定顺序 蓝→琥珀→青→紫；dark/light 两组。改动任一值须重跑
    specs/dashboard-v2-console/tools/validate_palette.js 保持 ALL PASS。"""
    html = _html()
    dark = ['#3b82f6', '#b45309', '#0891b2', '#7c3aed']
    light = ['#2563eb', '#b45309', '#0891b2', '#7c3aed']
    # 8 个值（含重复）逐字出现
    for hexv in dark + light:
        assert hexv in html, "palette hex missing from charts.js: %s" % hexv
    # 两组调色板作为连续数组字面量出现（强断言：顺序与分组正确）
    assert ("dark:  ['#3b82f6', '#b45309', '#0891b2', '#7c3aed']" in html), \
        "dark palette array literal not inlined verbatim"
    assert ("light: ['#2563eb', '#b45309', '#0891b2', '#7c3aed']" in html), \
        "light palette array literal not inlined verbatim"


def test_charts_js_node_syntax():
    """charts.js 必须通过 node --check 语法校验（零构建工具链，CI 前置门）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = ROOT / "inferfabric" / "dashboard" / "js" / "charts.js"
    proc = subprocess.run([node, "--check", str(js)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "node --check charts.js failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


def test_theme_state_hooked_in_store():
    """store.js 必须把主题状态注入 store('theme') 供 charts.js 订阅重建。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "store.js").read_text(encoding="utf-8")
    # toggleTheme 与 initTheme 都 set 'theme'
    assert "store.set('theme', 'dark')" in js
    assert "store.set('theme', 'light')" in js
    html = _html()
    # charts.js 订阅 store.on('theme')
    assert "store.on('theme'" in html or "window.store.on('theme'" in html


# ── Task 5: 监控 TAB（纯遥测，只读） ────────────────────────────────


def test_monitor_structure():
    """监控页 DOM 契约：7 个面板 id 必须在 get_html() 中出现（spec §4.3）。"""
    html = _html()
    for panel_id in (
        'id="monGpuChart"',     # GPU vram+util 时间曲线
        'id="monTokenChart"',   # Token prompt/completion 堆叠条
        'id="monLatencyChart"', # 延迟 P50/P95 双线
        'id="monKpis"',         # 五联 KPI
        'id="monLogTable"',     # 请求日志表
        'id="monHistTable"',    # 切换历史表
        'id="monCostCard"',     # 费用概览卡
    ):
        assert panel_id in html, "missing monitor panel id: %s" % panel_id
    # 窗口/粒度切换按钮 data-win / data-gran 契约（display filter）
    for win in ('1h', '24h', '7d'):
        assert 'data-win="%s"' % win in html, "missing GPU window toggle: %s" % win
    for gran in ('hour', 'day', 'month'):
        assert 'data-gran="%s"' % gran in html, "missing token granularity toggle: %s" % gran


def test_monitor_js_present():
    """monitor.js 模块存在并被 get_html() 内联装配（tabRenderers 注册）。

    断言：文件存在；get_html() 含 tabRenderers['tab-monitor'] 注册；
    三图通过 IFCharts.create 创建（null-check）；store sync_meta 订阅。"""
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js"
    assert js_path.exists(), "monitor.js missing"
    html = _html()
    assert "window.tabRenderers['tab-monitor'] = renderMonitor" in html, (
        "monitor.js tab renderer registration not inlined into HTML"
    )
    js = js_path.read_text(encoding="utf-8")
    # 三图均通过 IFCharts.create 创建（null-check each）
    assert "IFCharts.create('monGpuChart')" in js
    assert "IFCharts.create('monTokenChart')" in js
    assert "IFCharts.create('monLatencyChart')" in js
    # 订阅 store sync_meta（snapshot 到达时刷新）
    assert "store.on('sync_meta'" in js
    # 事件委托：窗口/粒度切换
    assert "data-win" in js and "data-gran" in js


def test_monitor_readonly():
    """监控 TAB 必须只读：monitor.js 不得包含任何 method:"POST" /
    method: "POST" 引用（spec §4.3：纯遥测、零操作）。

    窗口/粒度切换是 display filter（客户端过滤），不触达服务端状态。
    所有 fetch 调用必须为 GET（不指定 method = GET）。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js").read_text(encoding="utf-8")
    # 不得出现 POST method（含空格变体）
    assert 'method:"POST"' not in js, "monitor.js contains method:\"POST\" — read-only violation"
    assert 'method: "POST"' not in js, "monitor.js contains method: \"POST\" — read-only violation"
    assert "'POST'" not in js, "monitor.js references 'POST' — read-only violation"
    assert '"POST"' not in js, "monitor.js references \"POST\" — read-only violation"
    # HTML fragment 同样不得有 form action 或 method
    frag = (_DASHBOARD_DIR / "fragments" / "monitor.html").read_text(encoding="utf-8")
    assert "method=" not in frag, "monitor.html contains method= — read-only violation"
    assert "<form" not in frag, "monitor.html contains <form> — read-only violation"


def test_monitor_js_node_syntax():
    """monitor.js 必须通过 node --check 语法校验（零构建工具链，CI 前置门）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js"
    proc = subprocess.run([node, "--check", str(js)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "node --check monitor.js failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


def test_monitor_chart_series_limit():
    """每张图表系列数 ≤4（spec §7 / Task 4 review 约束：
    IFCharts 不折叠第 5 系列，会循环回蓝色，故调用方必须自行限制 ≤4）。

    断言 monitor.js 中 series 数组字面量每处 ≤4 个元素。这里用保守启发：
    检查每处 'series:' 后的数组，确认没有超过 4 个 { type: 行。"""
    import re
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js").read_text(encoding="utf-8")
    # 找所有 series: [ ... ] 块，统计每块内的 type: 出现次数
    for m in re.finditer(r'series:\s*\[', js):
        # 取到匹配的 ] 为止（简单扫描）
        start = m.end()
        depth = 1
        i = start
        while i < len(js) and depth > 0:
            if js[i] == '[':
                depth += 1
            elif js[i] == ']':
                depth -= 1
            i += 1
        block = js[start:i - 1]
        type_count = len(re.findall(r'type:\s*[\'"]', block))
        assert type_count <= 4, (
            "monitor.js series block has %d series (>4): %s" % (type_count, block[:120])
        )


def test_monitor_gpu_ring_buffer_continuous_accumulation():
    """GPU ring buffer 必须在每次 sync_meta（snapshot 到达）时持续积累样本，
    而非一次性种子后停止。

    回归守卫（fix round 1）：原实现用 _gpuBufSeeded one-shot 标志守护
    sync_meta handler 的 pushGpuSample()，且 renderGpuChart 内另有一次
    无条件 pushGpuSample()。结果：背景积累只触发一次（种子），之后仅
    monitor tab 活跃时才采样——切到 24h/7d 窗口只能看到几分钟会话数据。

    断言（源文本级，逻辑为订阅驱动难以隔离单测）：
    1. 不得存在 _gpuBufSeeded（或任何 *Seeded one-shot 种子标志）。
    2. renderGpuChart 函数体内不得调用 pushGpuSample（采样统一由
       sync_meta handler 负责，避免 tab 活跃时双倍写入）。
    3. sync_meta 订阅 handler 必须（无条件或仅 gpu 存在性检查后）调用
       pushGpuSample——不得被 one-shot 标志门控。"""
    import re
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js").read_text(encoding="utf-8")

    # 1. 无 one-shot 种子标志（_gpuBufSeeded 或任何 *Seeded 变量）
    seeded_refs = re.findall(r'\b\w*Seeded\b', js)
    assert not seeded_refs, (
        "monitor.js contains one-shot seed flag(s) %r — background GPU "
        "accumulation must be continuous, not gated by a seed flag" % seeded_refs
    )

    # 2. renderGpuChart 函数体内不得调用 pushGpuSample
    #    提取 renderGpuChart 函数体（到下一个顶层 function/window. 为止）
    m = re.search(r'function\s+renderGpuChart\s*\(\)\s*\{', js)
    assert m, "renderGpuChart function not found in monitor.js"
    body_start = m.end()
    # 扫描到匹配的 } （跟踪花括号深度）
    depth = 1
    i = body_start
    while i < len(js) and depth > 0:
        if js[i] == '{':
            depth += 1
        elif js[i] == '}':
            depth -= 1
        i += 1
    render_body = js[body_start:i - 1]
    assert 'pushGpuSample' not in render_body, (
        "renderGpuChart calls pushGpuSample — sampling must be solely via "
        "the sync_meta handler to avoid double-sampling when tab is active"
    )

    # 3. sync_meta 订阅 handler 必须调用 pushGpuSample，且不得被种子标志门控
    #    （允许 gpu 存在性检查，但不允许 !seeded 之类的 one-shot 守卫）
    sync_block = re.search(
        r"store\.on\('sync_meta'[^}]*?pushGpuSample[^}]*?\}", js, re.S
    )
    assert sync_block, (
        "sync_meta handler does not call pushGpuSample — GPU ring buffer "
        "won't accumulate on snapshot polls"
    )
    sync_text = sync_block.group(0)
    # 不得含 one-shot 守卫（!seeded / && !flag 之类）
    assert not re.search(r'!\s*\w*Seeded', sync_text), (
        "sync_meta handler gates pushGpuSample with a one-shot seed guard — "
        "background accumulation must fire on EVERY snapshot: %s" % sync_text
    )
    # 确认 handler 确实调用了 pushGpuSample
    assert 'pushGpuSample()' in sync_text, (
        "sync_meta handler does not invoke pushGpuSample(): %s" % sync_text
    )


# ── Task 6: 推理 TAB（模型卡片 + 网关控制卡） ──────────────────────


def test_inference_structure():
    """推理页 DOM 契约：网关控制卡 + 缓存控件 + 速率限制 + 三组模型容器。

    断言 get_html() 含：
      - #gwCard（网关控制卡）
      - #cacheToggle（LRU 缓存开关按钮）
      - #cacheHits（缓存信息标签）
      - #rlMeta（速率限制元信息）
      - 三组模型容器（独占/共享/空闲）
    """
    html = _html()
    for el in (
        'id="gwCard"',
        'id="cacheToggle"',
        'id="cacheHits"',
        'id="rlMeta"',
        'id="infExclGroup"',
        'id="infShrdGroup"',
        'id="infFreeGroup"',
    ):
        assert el in html, "missing inference contract id: %s" % el
    # 部署入口按钮（data-action 契约）
    assert 'data-action="goto-deploy"' in html, "missing deploy entry button"
    # 缓存开关 data-action 契约
    assert 'data-action="cache-toggle"' in html, "missing cache toggle data-action"


def test_inference_js_present():
    """inference.js 模块存在并被 get_html() 内联装配（tabRenderers 注册 + doModelAction）。

    断言：文件存在；get_html() 含 tabRenderers['tab-inference'] 注册；
    doModelAction 全局函数；四个操作端点契约；store sync_meta 订阅。"""
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "inference.js"
    assert js_path.exists(), "inference.js missing"
    html = _html()
    assert "window.tabRenderers['tab-inference'] = renderInference" in html, (
        "inference.js tab renderer registration not inlined into HTML"
    )
    assert "window.doModelAction" in html, (
        "window.doModelAction not inlined into HTML"
    )
    # 四个模型操作端点契约
    for ep in ("'/switch'", "'/stop'", "'/sleep'", "'/wake'"):
        assert ep in html, "inference.js missing endpoint: %s" % ep
    # 缓存开关端点契约
    assert "'/admin/cache/toggle'" in html, "inference.js missing cache toggle endpoint"
    # sync_meta 订阅（snapshot 到达时刷新）
    assert "store.on('sync_meta'" in html or "window.store.on('sync_meta'" in html
    # admin header 调用（UI.adminHeaders，R6）
    assert "UI.adminHeaders()" in html


def test_inference_no_inline_onclick():
    """inference.html 不得含任何 inline onclick= 属性（事件委托约束）。"""
    frag = (_DASHBOARD_DIR / "fragments" / "inference.html").read_text(encoding="utf-8")
    assert "onclick=" not in frag, (
        "inference.html contains inline onclick= — must use event delegation"
    )


def test_inference_js_node_syntax():
    """inference.js 必须通过 node --check 语法校验（零构建工具链，CI 前置门）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = ROOT / "inferfabric" / "dashboard" / "js" / "inference.js"
    proc = subprocess.run([node, "--check", str(js)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "node --check inference.js failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


def test_inference_no_fabricated_cache_hits():
    """#cacheHits 不得臆造命中计数 / 命中率（R10：后端不暴露）。

    inference.js 只展示缓存状态（开/关，来自 snapshot cache_enabled）+ 上限 500。
    不得出现 hit_rate / 命中率 / 命中数 等臆造字段，也不得从 /api/metrics
    读取命中计数。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "inference.js").read_text(encoding="utf-8")
    # 不得出现命中率字段
    assert "hit_rate" not in js, "inference.js fabricates hit_rate (R10 violation)"
    # 不得从 /api/metrics 读取 cache 命中数据（后端无此字段）
    import re
    metrics_fetch = re.findall(r"fetch\([^)]*api/metrics", js)
    assert not metrics_fetch, (
        "inference.js fetches /api/metrics for cache data (R10: no hits field exists)"
    )
    # cacheHits 文本必须含"上限"+ maxsize（信息性标签）
    assert "上限" in js, "cacheHits label missing maxsize info (R10)"
    assert "500" in js, "cacheHits label missing maxsize value 500"
    # cacheHits 文本只含状态 + 上限，不得含命中数变量插值
    # （合法文本：'LRU 缓存 · 开/关 · 上限 500 条'）
    assert "条" in js, "cacheHits label missing unit suffix"


def test_inference_no_new_api():
    """推理 TAB 不得新增任何 API 端点（R10/R11：API 冻结）。

    inference.js 的 fetch 调用必须仅指向既有端点：
    /switch /stop /sleep /wake /admin/cache/toggle。
    不得出现 /api/cache-hits /api/rate-limit 等新端点。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "inference.js").read_text(encoding="utf-8")
    import re
    # 提取所有 fetch 调用的 URL
    urls = re.findall(r"fetch\(\s*['\"]([^'\"]+)['\"]", js)
    allowed = {'/switch', '/stop', '/sleep', '/wake', '/admin/cache/toggle'}
    for u in urls:
        assert u in allowed, (
            "inference.js fetches non-allowed endpoint %r (API must stay frozen)" % u
        )


# ── Task 7: 部署 TAB（部署表单 + 拉取表单 + 长任务进度态） ──────────


def test_deploy_structure():
    """部署页 DOM 契约：部署表单卡 + 拉取表单卡 + 长任务进度态。

    断言 get_html() 含（R12：仅 name + engine 类型，无 dead fields）：
      - 部署表单：#depName（name input）、#depType（engine type select）、
        data-action="deploy"（部署按钮）
      - 拉取表单：#pullName（name input）、#pullFw（framework select）、
        data-action="pull"（拉取按钮）
      - #deployProgress（长任务进度态容器）
    """
    html = _html()
    for el in (
        'id="depName"',          # 部署表单：模型名称输入
        'id="depType"',          # 部署表单：引擎类型选择
        'data-action="deploy"',  # 部署表单：部署按钮
        'id="pullName"',         # 拉取表单：模型名称输入
        'id="pullFw"',           # 拉取表单：框架选择
        'data-action="pull"',    # 拉取表单：拉取按钮
        'id="deployProgress"',   # 长任务进度态容器
    ):
        assert el in html, "missing deploy contract id: %s" % el
    # 引擎类型选项契约（R14：仅 auto_deploy 支持的类型 vllm + ollama_cpp；
    # sglang/ninfer/ollama 服务不被 auto_deploy 支持，提供即误导——会返回 200+error）
    frag = (_DASHBOARD_DIR / "fragments" / "deploy.html").read_text(encoding="utf-8")
    for opt in ('value="vllm"', 'value="ollama_cpp"'):
        assert opt in frag, "missing auto-deployable engine option: %s" % opt
    for bad in ('value="sglang"', 'value="ninfer"'):
        assert bad not in frag, (
            "deploy.html offers %r — auto_deploy rejects it (R14: only vllm/ollama_cpp)" % bad
        )


def test_deploy_js_present():
    """deploy.js 模块存在并被 get_html() 内联装配（tabRenderers 注册 + doDeploy/doPull）。

    断言：文件存在；get_html() 含 tabRenderers['tab-deploy'] 注册；
    doDeploy / doPull 全局函数；两个端点契约；admin header 调用。"""
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "deploy.js"
    assert js_path.exists(), "deploy.js missing"
    html = _html()
    assert "window.tabRenderers['tab-deploy'] = renderDeploy" in html, (
        "deploy.js tab renderer registration not inlined into HTML"
    )
    assert "window.doDeploy" in html, "window.doDeploy not inlined into HTML"
    assert "window.doPull" in html, "window.doPull not inlined into HTML"
    # 两个端点契约
    assert "'/deploy'" in html, "deploy.js missing /deploy endpoint"
    assert "'/pull'" in html, "deploy.js missing /pull endpoint"
    # admin header 调用（R6）
    assert "UI.adminHeaders()" in html
    # 请求体契约：{name, type} 与 {name, framework}（R12）
    assert "name: name, type: type" in html or "name:name,type:type" in html, (
        "deploy.js must POST {name, type} (R12)"
    )
    assert "name: name, framework: fw" in html or "name:name,framework:fw" in html, (
        "deploy.js must POST {name, framework} (R12)"
    )
    # UI.confirm 在部署与拉取前调用（破坏性/长操作确认）
    js = js_path.read_text(encoding="utf-8")
    assert js.count("UI.confirm(") >= 2, (
        "deploy.js must call UI.confirm before both deploy and pull"
    )
    # 成功后跳转推理 TAB（spec §4.4）
    assert "switchTab('tab-inference')" in js, (
        "deploy.js must switch to inference tab on success"
    )


def test_deploy_no_dead_fields():
    """部署表单不得含旧 dead fields（R12）：model_dir / port / gpu_mem / slider。
    后端 auto_deploy 自动生成 YAML，不读这些字段（handler.py:1055 _handle_deploy
    仅取 name + type）。旧字段误导用户以为可配置部署参数。"""
    import re
    frag = (_DASHBOARD_DIR / "fragments" / "deploy.html").read_text(encoding="utf-8")
    # dead field 标识符不得出现（含 id= / name= 形式或裸标识符）
    for dead in ("model_dir", "gpu_mem", "slider"):
        assert dead not in frag, (
            "deploy.html contains dead field %r (R12: removed — backend auto-generates)" % dead
        )
    # port 不得作为表单字段标识出现（id="...port" / name="port"）
    assert not re.search(r'(id|name)\s*=\s*"[^"]*port[^"]*"', frag, re.I), (
        "deploy.html contains a 'port' form field (R12: dead field removed)"
    )


def test_deploy_no_inline_onclick():
    """deploy.html 不得含任何 inline onclick= 属性（事件委托约束）。"""
    frag = (_DASHBOARD_DIR / "fragments" / "deploy.html").read_text(encoding="utf-8")
    assert "onclick=" not in frag, (
        "deploy.html contains inline onclick= — must use event delegation"
    )


def test_deploy_js_node_syntax():
    """deploy.js 必须通过 node --check 语法校验（零构建工具链，CI 前置门）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = ROOT / "inferfabric" / "dashboard" / "js" / "deploy.js"
    proc = subprocess.run([node, "--check", str(js)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "node --check deploy.js failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


# ── Task 8: 云端 TAB（预设网格 + provider 表 + 手动配置，零 emoji） ──

# 常见 emoji 区段（U+1F300-1FAFF 符号 + U+2600-27BF 杂项/制表符号，
# 覆盖 legacy 的 ✓ ✗ 🔍 👁 🔧 与 preset icon 的 🟦🌋🐋🌙🟢🟠🔗）
_CLOUD_EMOJI_RE = __import__("re").compile(r"[\U0001F300-\U0001FAFF☀-➿]")
_CLOUD_LEGACY_EMOJI = [
    "\U0001F50D", "\U0001F441", "\U0001F527",   # 🔍 👁 🔧
    "✓", "✗",                                       # 状态标记
    "\U0001F7E6", "\U0001F30B", "\U0001F40B",     # 🟦 🌋 🐋
    "\U0001F319", "\U0001F7E2", "\U0001F410",     # 🌙 🟢 🔗
]


def _cloud_sources():
    frag = (_DASHBOARD_DIR / "fragments" / "cloud.html").read_text(encoding="utf-8")
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "cloud.js").read_text(encoding="utf-8")
    return frag, js


def test_cloud_structure():
    """云端页 DOM 契约（spec §4.5）：预设网格 + 内联 Key 表单 + provider 表
    + 手动配置表单 + 发现模型区。

    断言 get_html() 含契约 id（presetGrid, presetForm, provTable, cpName,
    cpApiKey, cpOpenaiBase, cpAnthropicBase, cloudModels）与静态 data-action 值
    （行级 provider-test/provider-delete/preset-select 由 cloud.js 动态渲染）。"""
    html = _html()
    for el in (
        'id="presetGrid"',       # 预设网格容器
        'id="presetForm"',       # 内联 API Key 表单
        'id="provTable"',        # provider 表
        'id="cpName"',
        'id="cpApiKey"',
        'id="cpOpenaiBase"',
        'id="cpAnthropicBase"',
        'id="cloudModels"',      # 发现模型区
    ):
        assert el in html, "missing cloud contract id: %s" % el
    # 静态 data-action 契约
    for act in ("preset-add", "preset-cancel", "manual-test", "manual-add",
                "reload", "provider-discover"):
        assert 'data-action="%s"' % act in html, "missing cloud data-action: %s" % act


def test_cloud_js_present():
    """cloud.js 模块存在且被 get_html() 内联装配（tabRenderers 注册 + 5 端点
    + 6 个暴露函数 + UI.adminHeaders）。"""
    import re
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "cloud.js"
    assert js_path.exists(), "cloud.js missing"
    html = _html()
    assert "window.tabRenderers['tab-cloud'] = renderCloud" in html, (
        "cloud.js tab renderer registration not inlined into HTML"
    )
    # 五个端点契约（API 冻结，不得新增）
    for ep in ("'/admin/cloud/presets'", "'/admin/cloud/providers'",
               "'/admin/cloud/test'", "'/admin/cloud/discover'", "'/admin/cloud/reload'"):
        assert ep in html, "cloud.js missing endpoint: %s" % ep
    assert "UI.adminHeaders()" in html, "cloud.js must use UI.adminHeaders()"
    # 暴露的 6 个操作函数
    for fn in ("window.doCloudAdd", "window.doCloudAddPreset", "window.doCloudTest",
               "window.doCloudDiscover", "window.doCloudDelete", "window.doCloudReload"):
        assert fn in html, "cloud.js missing global: %s" % fn
    # 动态渲染的 data-action 契约（预设卡 / 行内 测试 / 行内 删除）
    js = js_path.read_text(encoding="utf-8")
    for act in ("preset-select", "provider-test", "provider-delete"):
        assert 'data-action="%s"' % act in js or "'%s'" % act in js, (
            "cloud.js missing data-action: %s" % act
        )
    # 请求体契约：preset 路径 {preset, api_key}；手动路径 {name, api_key, openai_base, anthropic_base}
    assert "preset: preset.id, api_key: apiKey" in js or "preset: preset.id" in js, (
        "cloud.js must POST {preset, api_key} for preset path"
    )
    assert re.search(r"openai_base:\s*openaiBase,\s*anthropic_base:\s*anthropicBase", js), (
        "cloud.js must POST {name, api_key, openai_base, anthropic_base} for manual path"
    )


def test_cloud_no_emoji():
    """cloud.html + cloud.js 零 emoji（R13）：无常见 emoji 区段码位、
    无 legacy emoji（🔍👁🔧✓✗🟦🌋🐋🌙🟢🟠🔗）；
    且预设卡不得渲染 preset 的 icon 字段（p.icon 不进入 innerHTML）。"""
    import re
    frag, js = _cloud_sources()
    for src, name in ((frag, "cloud.html"), (js, "cloud.js")):
        m = _CLOUD_EMOJI_RE.search(src)
        assert not m, "%s contains emoji codepoint U+%04X" % (name, ord(m.group(0)))
        for ch in _CLOUD_LEGACY_EMOJI:
            assert ch not in src, "%s contains legacy emoji %r" % (name, ch)
    # preset icon 字段（emoji）不得被渲染：p.icon / pr.icon 不得出现
    assert "p.icon" not in js, "cloud.js renders preset icon field (p.icon)"
    assert "pr.icon" not in js, "cloud.js renders preset icon field (pr.icon)"
    # 除 UI.icon( SVG 助手外，不得出现 .icon 字段访问
    assert not re.search(r"\.icon\b", js.replace("UI.icon", "")), (
        "cloud.js renders .icon field into HTML (R13: preset icon is emoji)"
    )


def test_cloud_delete_uses_confirm():
    """cloudDeleteProvider 必须走 UI.confirm danger 确认，不得使用原生 confirm()。"""
    js = _cloud_sources()[1]
    assert "UI.confirm(" in js, "cloud.js must use UI.confirm for provider deletion"
    assert "danger: true" in js, "cloud.js deletion confirm must be danger"
    # 移除 UI.confirm( 后不得残留任何 confirm( 调用（原生 confirm 违规）
    stripped = js.replace("UI.confirm(", "")
    assert "confirm(" not in stripped, (
        "cloud.js uses native confirm() — must use UI.confirm (R13)"
    )


def test_cloud_no_inline_onclick():
    """cloud.html 不得含任何 inline onclick= 属性（事件委托约束）。"""
    frag = _cloud_sources()[0]
    assert "onclick=" not in frag, (
        "cloud.html contains inline onclick= — must use event delegation"
    )


def test_cloud_js_node_syntax():
    """cloud.js 必须通过 node --check 语法校验（零构建工具链，CI 前置门）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = ROOT / "inferfabric" / "dashboard" / "js" / "cloud.js"
    proc = subprocess.run([node, "--check", str(js)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "node --check cloud.js failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


def test_deploy_success_guard():
    """doDeploy 不得仅凭 HTTP 200 判定成功——后端 _handle_deploy 对逻辑失败也返回
    200（_send_json 默认 200）。必须查 j.status：成功仅 {switched, already_active}
    （model_lifecycle.py:333 / manager.py:399）；其余（"Unsupported type for
    auto-deploy" / "Unknown model" / "Invalid transition"）须走失败分支。

    回归守卫：若有人改回 `if (res.ok)` 即成功，下面 'switched'/'already_active'
    字面量与 j.status 检查会消失，测试失败。
    """
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "deploy.js").read_text(encoding="utf-8")
    # 必须检查响应体 status（不能只看 res.ok）
    assert "j.status" in js, "doDeploy must inspect j.status (backend returns 200 on logical errors)"
    # 成功状态白名单（与 model_lifecycle.py:333 一致）
    assert "'switched'" in js, "doDeploy success set must include 'switched'"
    assert "'already_active'" in js, "doDeploy success set must include 'already_active'"
    # 非白名单状态须抛错走失败分支（不能静默成功）
    assert "throw new Error" in js, "doDeploy must throw on non-success status"


# ── Task 9: 异常 TAB（结构化事件表 + 类别/严重度过滤 + 全文搜索 + critical 高亮） ──

# 异常页 emoji 区段（与 cloud 同口径：U+1F300-1FAFF 符号 + U+2600-27BF 杂项/制表符号）
_ANOM_EMOJI_RE = __import__("re").compile(r"[\U0001F300-\U0001FAFF☀-➿]")
_ANOM_LEGACY_EMOJI = [
    "\U0001F50D", "\U0001F441", "\U0001F527",   # 🔍 👁 🔧
    "✓", "✗",                                       # 状态标记
    "\U0001F7E2", "\U0001F7E1", "\U0001F534",     # 🟢 🟡 ⚫（旧 anomaly 严重度 emoji）
]


def _anomaly_sources():
    frag = (_DASHBOARD_DIR / "fragments" / "anomaly.html").read_text(encoding="utf-8")
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "anomaly.js").read_text(encoding="utf-8")
    return frag, js


def test_anomaly_structure():
    """异常页 DOM 契约（spec §4.6）：事件表 + 过滤栏 + 计数。

    断言 get_html() 含契约 id（anomTable/anomTbody/anomCatFilter/anomSevFilter/
    anomSearch/anomCount）与过滤选项值（category 5 值 + severity 4 值）。"""
    html = _html()
    for el in (
        'id="anomTable"',       # 事件表
        'id="anomTbody"',       # 事件表 tbody
        'id="anomCatFilter"',   # 类别过滤
        'id="anomSevFilter"',   # 严重度过滤
        'id="anomSearch"',      # 全文搜索
        'id="anomCount"',       # 总计数
    ):
        assert el in html, "missing anomaly contract id: %s" % el
    frag = (_DASHBOARD_DIR / "fragments" / "anomaly.html").read_text(encoding="utf-8")
    for opt in ('value="routing"', 'value="model"', 'value="auth"',
                'value="config"', 'value="cloud"'):
        assert opt in frag, "missing category option: %s" % opt
    for opt in ('value="info"', 'value="warning"', 'value="error"', 'value="critical"'):
        assert opt in frag, "missing severity option: %s" % opt


def test_anomaly_js_present():
    """anomaly.js 模块存在且被 get_html() 内联装配（tabRenderers 注册 + 端点 + fetch）。

    断言：文件存在；get_html() 含 tabRenderers['tab-anomaly'] 注册；
    /api/anomalies 端点契约；fetch 调用存在（/api/anomalies 为公开只读端点，无需 admin header）。"""
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "anomaly.js"
    assert js_path.exists(), "anomaly.js missing"
    html = _html()
    assert "window.tabRenderers['tab-anomaly'] = renderAnomaly" in html, (
        "anomaly.js tab renderer registration not inlined into HTML"
    )
    js = js_path.read_text(encoding="utf-8")
    assert "'/api/anomalies'" in js, "anomaly.js missing /api/anomalies endpoint"
    # /api/anomalies 为公开只读 GET：允许 UI.adminHeaders() 或裸 fetch，二者其一即可
    assert ("UI.adminHeaders()" in js) or ("fetch(" in js), (
        "anomaly.js must fetch /api/anomalies (UI.adminHeaders or fetch)"
    )
    # 暴露的操作函数
    for fn in ("window.renderAnomaly", "window.doAnomRefresh"):
        assert fn in html, "anomaly.js missing global: %s" % fn


def test_anomaly_no_emoji():
    """anomaly.html + anomaly.js 零 emoji：无常见 emoji 区段码位、无 legacy 严重度
    emoji（🟢🟡⚫ 等）——严重度一律 SVG 图标 + 文字标签双编码。"""
    frag, js = _anomaly_sources()
    for src, name in ((frag, "anomaly.html"), (js, "anomaly.js")):
        m = _ANOM_EMOJI_RE.search(src)
        assert not m, "%s contains emoji codepoint U+%04X" % (name, ord(m.group(0)))
        for ch in _ANOM_LEGACY_EMOJI:
            assert ch not in src, "%s contains legacy emoji %r" % (name, ch)


def test_anomaly_no_inline_onclick():
    """anomaly.html 不得含任何 inline onclick= 属性（事件委托约束）。"""
    frag = (_DASHBOARD_DIR / "fragments" / "anomaly.html").read_text(encoding="utf-8")
    assert "onclick=" not in frag, (
        "anomaly.html contains inline onclick= — must use event delegation"
    )


def test_anomaly_js_node_syntax():
    """anomaly.js 必须通过 node --check 语法校验（零构建工具链，CI 前置门）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = ROOT / "inferfabric" / "dashboard" / "js" / "anomaly.js"
    proc = subprocess.run([node, "--check", str(js)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "node --check anomaly.js failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


def test_anomaly_critical_highlight():
    """critical 行须整行高亮：anomaly.js 对 severity==='critical' 追加 row-crit 类
    （spec §4.6；row-crit 为低 alpha --crit 背景 tint，文字色不变）。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "anomaly.js").read_text(encoding="utf-8")
    assert "row-crit" in js, "anomaly.js must add row-crit class to critical rows"
    assert "'critical'" in js, "anomaly.js must branch on severity === 'critical'"


# ── Task 10 收口断言集（巨石删除 / 骨架标准化 / 断连 banner / 全 TAB 装配） ──

def test_app_js_deleted():
    """legacy app.js 巨石（1432 行死代码）已删除；JS 模块化为 9 个模块（无 app）。"""
    assert not (ROOT / "inferfabric" / "dashboard" / "js" / "app.js").exists(), (
        "legacy app.js monolith must be deleted (Task 10)"
    )
    import inferfabric.dashboard as dash
    assert "app" not in getattr(dash, "JS_MODULES", ["app"]), (
        "app must not be in JS_MODULES"
    )


def test_no_app_js_reference():
    """served HTML（get_html() 内联全部 JS）不得含 app.js 字面引用——
    含 store.js/ui.js 内的历史注释（已改写为“旧巨石”表述）。"""
    html = _html()
    assert "app.js" not in html, (
        "served HTML still references app.js (stale comment or script tag)"
    )


def test_all_tab_renderers_registered():
    """全部 6 个 TAB 的 tabRenderers 注册必须出现在 served HTML（Task 10 收口）。"""
    html = _html()
    for tab in ("tab-overview", "tab-inference", "tab-monitor",
                "tab-deploy", "tab-cloud", "tab-anomaly"):
        assert "window.tabRenderers['%s']" % tab in html, (
            "missing tabRenderers registration: %s" % tab
        )


def test_skeleton_in_data_driven_tabs():
    """spec §5 全 TAB 骨架态：数据驱动区域（cloud 预设网格 / provider 表、
    anomaly 事件表）用 UI.skeleton + .skeleton 占位，不得残留静态“加载中…”。
    deploy TAB 为静态表单（无数据依赖）——不要求骨架。"""
    js_dir = ROOT / "inferfabric" / "dashboard" / "js"
    cloud_js = (js_dir / "cloud.js").read_text(encoding="utf-8")
    anomaly_js = (js_dir / "anomaly.js").read_text(encoding="utf-8")
    assert "UI.skeleton" in cloud_js, "cloud.js must call UI.skeleton on initial load"
    assert "UI.skeleton" in anomaly_js, "anomaly.js must call UI.skeleton on load"
    frag_dir = ROOT / "inferfabric" / "dashboard" / "fragments"
    for name in ("cloud.html", "anomaly.html", "deploy.html"):
        frag = (frag_dir / name).read_text(encoding="utf-8")
        assert "加载中" not in frag, (
            "%s still uses static 加载中 placeholder (use skeleton)" % name
        )
    cloud_frag = (frag_dir / "cloud.html").read_text(encoding="utf-8")
    anomaly_frag = (frag_dir / "anomaly.html").read_text(encoding="utf-8")
    assert 'class="skeleton"' in cloud_frag, "cloud.html initial markup must be skeleton"
    assert 'class="skeleton"' in anomaly_frag, "anomaly.html initial markup must be skeleton"


def test_api_error_banner_last_sync_text():
    """spec §5 断连 banner：#apiErrorBanner 显示时须带“最后数据更新于 X”
    （X = sync_meta.ts，最近一次成功同步时间；从未同步过则 “—”）。"""
    html = _html()
    assert 'id="apiErrorBanner"' in html, "disconnect banner missing from shell"
    assert "最后数据更新于" in html, (
        "store.js api_error handler must show 最后数据更新于 X in banner"
    )
    frag = (_DASHBOARD_DIR / "base.html").read_text(encoding="utf-8")
    assert "apiErrorBanner" in frag
    assert "重试" in frag, "banner must offer a retry action"


