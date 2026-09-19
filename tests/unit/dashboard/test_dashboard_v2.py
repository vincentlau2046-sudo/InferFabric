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

