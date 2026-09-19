"""Dashboard v2 (InferFabric Console) — 结构断言。增量随任务扩展。"""
from pathlib import Path
from inferfabric.dashboard import get_html

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
