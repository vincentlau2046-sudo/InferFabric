# Dashboard v2 Console 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 dashboard 重做成专业监控台（InferFabric Console）：dark/light 双主题令牌、GPU 遥测带主角元素、ECharts 时间序列、六 TAB 信息架构重排，零构建零 webfont。

**Architecture:** 保留 `inferfabric/dashboard/` 的 fragment 拼装机制与 `get_html()` API（代理 `:8999` 同源服务，`_serve_dashboard` 向 `</head>` 前注入 `window.__TOKEN_STATS__`，见 `handler.py:216-224`）。CSS/HTML/JS 全部重写；JS 从 1432 行 `app.js` 巨石拆为 ui/store/charts + 每 TAB 一个模块；ECharts 单文件 vendor 进 git。后端 API 面一个端点都不加。

**Tech Stack:** vanilla JS（ES2020，无构建）、ECharts 5.x（UMD 单文件 vendor）、系统字体栈（零 webfont/CDN）、pytest + `node --check` 双验证。

**Spec:** `specs/dashboard-v2-console/spec.md`（本计划的唯一设计依据；执行者两份都读）

## Global Constraints

- Python 3.10+；**不修改 proxy 转发核心路径**（PR-14 宪法约束）；`_serve_dashboard` 路由不动
- `get_html()` 拼装机制保留：`tests/unit/infra/test_core_comprehensive.py:1137`（断言 `"<html" in html` 且 `len > 1000`）必须继续通过
- `base.html` 必须保留**恰好一个** `</head>`（代理在 `</head>` 前注入 `__TOKEN_STATS__` 脚本，`handler.py:216-224`）
- ECharts vendor 放 `inferfabric/dashboard/vendor/` 并**纳入 git**（`_deps/` 是 untracked，禁止放那里）
- 零 webfont、零 CDN（机器离线可用）；全界面零 emoji（SVG symbol 替代）
- 数字一律等宽字体 + `font-variant-numeric: tabular-nums`
- 开发在 `sandbox/`（宪法治理），本计划所有 git 提交都在 sandbox 工作副本内；**仅在 Task 11 完成后**才 merge 生产
- 每个 Task 结束必须：① `python3 -m pytest tests/unit/dashboard/ tests/unit/infra/test_core_comprehensive.py -q` 绿 ② 对改动的 js 跑 `node --check` ③ commit

## File Structure

```
inferfabric/dashboard/
├── __init__.py            # 拼装器：JS_MODULES 显式列表（Task 1 改）
├── base.html              # 重写：shell + 顶栏 + 侧栏 + 遥测带 + fragment/JS 占位符（Task 1）
├── fragments/
│   ├── style.css          # 重写：双层令牌 + 组件（Task 1）
│   ├── overview.html      # 重写（Task 2 静态 / Task 3 接数据）
│   ├── inference.html     # 重写（Task 6）
│   ├── monitor.html       # 重写（Task 5）
│   ├── deploy.html        # 重写（Task 7）
│   ├── cloud.html         # 重写（Task 8）
│   └── anomaly.html       # 重写（Task 9）
├── js/
│   ├── ui.js              # 新建：helpers/format/toast/confirm modal/skeleton/图标工具（Task 1）
│   ├── store.js           # 新建：现 state.js 全量移植 + 主题初始化（Task 1）
│   ├── charts.js          # 新建：ECharts 实例工厂 + 双主题 option（Task 4）
│   ├── overview.js        # 新建（Task 3）
│   ├── inference.js       # 新建（Task 6）
│   ├── monitor.js         # 覆盖旧文件（Task 5）
│   ├── deploy.js          # 新建（Task 7）
│   ├── cloud.js           # 新建（Task 8）
│   └── anomaly.js         # 覆盖旧文件（Task 9）
└── vendor/
    └── echarts.min.js     # 新建（Task 4，git 跟踪）
删除：js/bindings.js、js/state.js、js/app.js（Task 10 删 app.js，Task 1 删 bindings/state）
tests/unit/dashboard/test_dashboard_v2.py   # 新建（Task 1 起增量扩展）
```

---

### Task 1: 设计令牌 + 图标集 + Shell + JS 骨架

**Files:**
- Modify: `inferfabric/dashboard/__init__.py`（JS 列表 → `JS_MODULES = ["ui", "store", "charts", "overview", "inference", "monitor", "deploy", "cloud", "anomaly"]`，缺失文件跳过并 warning——现逻辑保留）
- Rewrite: `inferfabric/dashboard/base.html`、`fragments/style.css`
- Create: `js/ui.js`、`js/store.js`
- Delete: `js/bindings.js`、`js/state.js`
- Create: `tests/unit/dashboard/test_dashboard_v2.py`

**Interfaces:**
- Produces（后续任务依赖，签名必须一致）：
  - `window.store`：现 `state.js` 的 `StateStore` 全量移植——`get/set/update/on(key,cb)/fetchSnapshot(force)/forceRefresh()/startPolling(ms)/switchTab(id)/setSwitchLocked(bool)/isSwitchLocked()`，键名与归一化形状（`gpu_mode`/`gpu`/`gpu_util`/`mem`/`cpu`/`models`/`history`/`token_stats`/`request_log`/`metrics_24h`/`local_models`/`sync_meta`/`api_error`/`tab_active`）与现 state.js 完全一致（`state.js:109-170` 的 merged 形状照抄）
  - `window.UI`：`UI.toast(msg, kind)`、`UI.confirm({title, body, danger, onOk})`、`UI.skeleton(el, rows)`、`UI.fmtGB(mb)`、`UI.fmtDur(sec)`（= 现 `ovFormatUptime`）、`UI.fmtNum(n)`、`UI.icon(name)`（返回 `<svg><use href="#s-...">` 字符串）
  - CSS 令牌：`--bg --surface --surface-2 --border --ink --ink-2 --ink-3 --accent --ok --warn --crit --info`（dark 默认值 + `[data-theme="light"]` 覆盖，值见 spec §2）
  - 图标 symbol：扩到 ~25 个（现 12 个保留 id 不变，新增 `s-play s-stop s-pause s-download s-server s-key s-filter s-trash s-search s-wake s-sleep s-release s-shield s-database s-wifi`）
  - 遥测带容器：`#telemetryRail`（含 `#trVram #trVramFill #trUtil #trUtilFill #trPwr #trTemp #trClock`）
- Consumes: 现 `state.js` 全文（移植基准）、`base.html:10-68` 的 SVG symbol defs（扩集）

- [ ] **Step 1: 写失败测试**

`tests/unit/dashboard/test_dashboard_v2.py`（新建 `tests/unit/dashboard/` 目录）：

```python
"""Dashboard v2 (InferFabric Console) — 结构断言。增量随任务扩展。"""
from pathlib import Path
from inferfabric.dashboard import get_html

ROOT = Path(__file__).resolve().parents[2]

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
```

注意：`get_html()` 有进程内缓存；测试进程内首调即生效。

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/unit/dashboard/test_dashboard_v2.py -v`
Expected: FAIL（`test_shell_structure` 缺 `#telemetryRail` 等）

- [ ] **Step 3: 改 `__init__.py` 的 JS 列表**

把 `for js_name in ("bindings", "state", "app", "monitor", "anomaly")` 改为 `JS_MODULES` 显式列表（见 File Structure），缺失文件跳过 + `log.warning`（现行为保留）。

- [ ] **Step 4: 重写 `style.css`（令牌层 + 组件）**

按 spec §2/§3：primitive（色板原始值）→ semantic 两层；dark 为 `:root` 默认，`[data-theme="light"]` 仅覆盖 semantic。删掉现 macOS 拟物（窗口圆点样式、拟物阴影）。组件清单：card、KPI tile、telemetry-rail、status dot、progress bar、data table、button（`btn-pri/btn-sec/btn-danger`）、badge、toast、confirm modal、skeleton、empty state、skeleton shimmer（`prefers-reduced-motion` 下禁用）。字体：sans 栈 + `--font-mono: ui-monospace, 'SF Mono', Menlo, Consolas, monospace`；`b, .num { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }`。

- [ ] **Step 5: 重写 `base.html`**

- 顶栏：brand + GPU mode 徽标（`#modeBadge`，idle/shared/exclusive 三态配色）+ `#syncMeta` + `#refreshNow` + `#themeToggle`（SVG 图标，非 emoji）+ OpenAPI 链接
- 左侧 56px 图标导航（6 项，`data-tab` 属性保留），底部同步状态点
- 主区顶部：`#telemetryRail`（5 段：VRAM 值+容量+fill / 利用率+fill / 功耗 / 温度 / 时钟，段 id 见 Interfaces）
- 6 个 fragment 占位符 + JS 占位符按新 `JS_MODULES` 顺序；toast / apiErrorBanner / switchOverlay 保留（switchOverlay 文案逻辑在 store.js 里）
- 零 emoji；主题初始化脚本逻辑移入 store.js（现 state.js:322-333 移植，`updateThemeIcon` 改 SVG 切换）
- 保留恰好一个 `</head>`

- [ ] **Step 6: 写 `js/ui.js`**

按 Interfaces 的 `window.UI` 签名实现。`UI.confirm` 渲染进固定 modal 容器（`<div id="confirmModal">` 由 base.html 提供，默认 `display:none`）；`danger: true` 时主按钮为 `btn-danger` 红。`UI.skeleton(el, rows)` 生成 `rows` 行 shimmer 占位。`UI.icon(name, size=16)` 返回内联 svg 字符串。

- [ ] **Step 7: 写 `js/store.js`**

现 `state.js` 全量移植（逐行对照，行为不变）：StateStore 类 + `window.store` + 轮询 + ETag + 切换 overlay 订阅（`state.js:268-283`）+ sync 指示器（`285-299`）+ 主题初始化（`301-333`，`updateThemeIcon` 改双 SVG）。**新增** `window.refreshPanels` 钩子改为按 TAB 注册：`window.tabRenderers = {}`（`tabRenderers['tab-overview'] = fn`），`forceRefresh` 后调用 `tabRenderers[store.get('tab_active')]?.(snap)` 替代 `window.refreshPanels`（旧 app.js 的 `refreshPanels` 在 Task 10 随 app.js 删除）。

- [ ] **Step 8: 六个 fragment 先置空壳**

`fragments/{overview,inference,monitor,deploy,cloud,anomaly}.html` 每个写最小结构：`<div class="tab-content" id="tab-X">` + 一张 card + `UI.skeleton` 占位注释（实际 skeleton 由各 TAB 任务接管）。保证 shell 可跑、不破坏运行。

- [ ] **Step 9: 验证**

```bash
python3 -m pytest tests/unit/dashboard/ tests/unit/infra/test_core_comprehensive.py -q
node --check inferfabric/dashboard/js/ui.js && node --check inferfabric/dashboard/js/store.js
python3 -c "import inferfabric"
# 运行时冒烟（sandbox 内）
python3 -m inferfabric.proxy.handler & sleep 2 && curl -s localhost:8999/ | grep -c telemetryRail && curl -s localhost:8999/health
```

Expected: 全绿 + telemetryRail 命中。

- [ ] **Step 10: Commit**

```bash
git add inferfabric/dashboard/ tests/unit/dashboard/
git commit -m "feat(dashboard): v2 design tokens, icon set, shell + telemetry rail, JS module skeleton"
```

---

### Task 2: 总览页静态原型（用户过目门槛）

**Files:**
- Rewrite: `fragments/overview.html`
- Modify: `fragments/style.css`（遥测带 + 总览组件样式）

**Interfaces:**
- Produces: 总览页 DOM 契约（Task 3 接数据时用这些 id）：`#ovActiveCard #ovActiveBody #ovSpark24h #ovAnomTop #sysOpsCard`（按钮 `data-action="release|reconcile|reload|reset"`）
- 本任务全部用**假数据**（写死在 fragment 内），验收 = 视觉

- [ ] **Step 1: 写 fragment**

结构（顺序即视觉层级）：
1. GPU 遥测带填充假值（VRAM 24.1/32 GB · util 87% · 320 W · 68 °C · 2.41 GHz）
2. 第二行 12 列网格：`#ovActiveCard`（col 8）：模型名 Qwen3-32B（NInfer 徽标）/ `:8007` / up 3h12m / 操作按钮 stop·sleep·wake；`#ovSpark24h`（col 4）：24h 请求 sparkline（先用手写 SVG polyline 假数据占位，Task 3 换 ECharts）
3. 第三行：`#ovAnomTop`（col 8，最近异常 3 行：时间/级别图标/message/「查看全部」链接跳异常 TAB）+ `#sysOpsCard`（col 4：释放 GPU / Reconcile / 重载配置 / 强制重置，`btn-sec` 风格，重置 `btn-danger`）

- [ ] **Step 2: 视觉验证**

冒烟启动后浏览器打开 `http://127.0.0.1:8999/`（或用户指定方式），截图总览页（dark + light 各一）。

- [ ] **Step 3: 用户过目（硬门槛）**

把截图给用户；用户点头前**不进入 Task 3**。

- [ ] **Step 4: 补测试断言 + commit**

`test_dashboard_v2.py` 增 `test_overview_structure`（断言 `#ovActiveCard #ovSpark24h #ovAnomTop #sysOpsCard` 存在）。跑 Step 1 同款验证 + commit（message: `feat(dashboard): overview static prototype (telemetry rail + cards)`）。

---

### Task 3: 总览页接真实数据

**Files:**
- Create: `js/overview.js`
- Modify: `fragments/overview.html`（假值改占位）、`__init__.py` 无需动（JS_MODULES 已含 overview）

**Interfaces:**
- Consumes: `window.store`（键：`gpu gpu_util mem cpu models active_services sleep_states anomaly` 由 Task 9 前暂用 `/api/anomalies` 直接 fetch）、`window.UI`、`window.__TOKEN_STATS__`（代理注入的 token 统计原始状态，`handler.py:216-224`）
- Produces: `tabRenderers['tab-overview'] = renderOverview`（store.forceRefresh 钩子）；`window.doSystemOps(action)`（release→`POST /switch {model:"idle"}`、reconcile→`POST /reconcile`、reload→`POST /reload-config`、reset→`POST /reset`，均带 `UI.confirm` 确认 + `UI.toast` 结果 + `store.forceRefresh()`）

- [ ] **Step 1: 实现 `js/overview.js`**

```js
/* Overview tab — telemetry rail + active model + 24h spark + anomalies top3 + system ops */
(async function () {
  const $ = (id) => document.getElementById(id);
  function renderRail() {
    const s = window.store;
    const gpu = s.get('gpu') || {}, util = s.get('gpu_util') || {};
    // 填 #trVram/#trVramFill/#trUtil/#trUtilFill/#trPwr/#trTemp/#trClock
    // fill 宽度 = pct + '%'，>90% 加 .crit 类（颜色走 --crit）
  }
  function renderActive() { /* 从 models/active_services 找活跃项，渲染 #ovActiveBody */ }
  function renderSpark() {
    // window.__TOKEN_STATS__ 按小时聚合 24 桶 → 更新 #ovSpark24h 的 SVG polyline
    // （Task 4 之后可换 ECharts，容器 id 不变）
  }
  async function renderAnoms() { /* GET /api/anomalies 取 top3 → #ovAnomTop */ }
  window.renderOverview = () => { renderRail(); renderActive(); renderSpark(); renderAnoms(); };
  window.tabRenderers['tab-overview'] = renderOverview;
  window.store.on('sync_meta', () => { if (document.getElementById('tab-overview').classList.contains('active')) renderOverview(); });
  window.doSystemOps = async (action) => { /* 按 Interfaces 实现，reset 用 UI.confirm danger */ };
})();
```

（以上是骨架，实现时把注释块写全；按钮 `onclick="UI.confirm({...})"` 或 data-action 事件委托二选一，选事件委托。）

- [ ] **Step 2: 验证**

pytest + `node --check js/overview.js` + 冒烟：curl 总览页 HTML，`grep -c 'ovActiveBody'`；浏览器确认遥测带数字随 GPU 变化（跑一次 `./iff switch` 前后对比）。commit。

---

### Task 4: ECharts vendor + 双主题图表工厂 + CVD 调色板验证

**Files:**
- Create: `inferfabric/dashboard/vendor/echarts.min.js`（git 跟踪）
- Create: `js/charts.js`
- Modify: `base.html`（`<script src="/static/echarts.min.js">`？——**不**：走拼装，在 `__init__.py` 里给 ECharts 单独占位符 `<!-- JS:echarts -->` 内联，保持"零额外 HTTP 请求 + get_html 单文件"不变量）

**Interfaces:**
- Produces: `window.IFCharts.create(containerId, theme)`（返回 echarts 实例，主题 = 当前 `data-theme`）；`window.IFCharts.update(instanceId, optionPatch)`；`window.IFCharts.palettes`（`{dark: [...], light: [...]}` 四色系，固定顺序蓝/琥珀/绿/紫）；`window.IFCharts.onThemeChange(cb)`
- 图表规则（dataviz）：单 y 轴、线宽 2px、网格线退隐、crosshair tooltip、≥2 系列必有 legend、系列色固定顺序不循环

- [ ] **Step 1: 获取 ECharts**

```bash
cd /tmp && npm pack echarts@5.5.1 && tar xzf echarts-5.5.1.tgz
cp package/dist/echarts.min.js <sandbox>/inferfabric/dashboard/vendor/
node -e "const e=require('<abs>/vendor/echarts.min.js'); if(typeof e.init!=='function')process.exit(1); console.log('echarts OK', e.version)"
ls -la <sandbox>/inferfabric/dashboard/vendor/echarts.min.js   # 期望 > 900KB
```
无网络时：让用户手动提供 `echarts.min.js`（5.x UMD 版）放入 vendor/，再跑上面 node 校验。

- [ ] **Step 2: 定稿双调色板并跑 CVD 验证（FAIL 必须修）**

dark 组候选 `#58a6ff #e3b341 #3fb950 #bc8cff`（surface `#161c24`）；light 组候选 `#2563eb #b45309 #15803d #7c3aed`（surface `#ffffff`）。运行（dataviz 技能脚本）：

```bash
node ~/.claude/plugins/cache/*/dataviz/scripts/validate_palette.js "#58a6ff,#e3b341,#3fb950,#bc8cff" --mode dark
node ~/.claude/plugins/cache/*/dataviz/scripts/validate_palette.js "#2563eb,#b45309,#15803d,#7c3aed" --mode light
```
（脚本实际路径以 `Skill: dataviz` 加载时的 base directory 为准。）任何 FAIL：调相邻色阶再跑，直到全过；把最终 hex + 脚本输出摘要记入 `STATUS.md`。

- [ ] **Step 3: 写 `js/charts.js`**

`IFCharts` 按 Interfaces 实现：实例按主题创建（主题切换时 dispose 重建，不是 setOption 换色——spec §7）；option 基座：`grid` 紧凑、`axisLine` 隐藏、`splitLine` 用 `--border` 色、line 2px、`symbol: 'none'`、tooltip 十字线。ECharts 内联占位符加进 `__init__.py`（JS_MODULES 之前插入，缺失时 warning + 跳过，图表 TAB 显示 empty state）。

- [ ] **Step 4: 验证**

```bash
python3 -m pytest tests/unit/dashboard/ -q
node --check inferfabric/dashboard/js/charts.js
# echarts 内联后 get_html 体积：
python3 -c "from inferfabric.dashboard import get_html; print(len(get_html()))"   # 期望 ~1.3M
```
commit（含 vendor 文件；`git add inferfabric/dashboard/vendor/`）。

---

### Task 5: 监控 TAB（纯遥测，只读）

**Files:**
- Rewrite: `fragments/monitor.html`、`js/monitor.js`（覆盖旧文件）
- Modify: `__init__.py`（JS_MODULES 不变，monitor 已在列表）

**Interfaces:**
- Consumes: `IFCharts`（Task 4）、`store` 键 `metrics_24h request_log history token_stats`、`window.__TOKEN_STATS__`
- Produces: `tabRenderers['tab-monitor']`；本 TAB **不得出现任何操作按钮/开关**（验收项）；面板 id：`#monGpuChart #monTokenChart #monLatencyChart #monKpis #monLogTable #monHistTable #monCostCard`

- [ ] **Step 1: 重写 fragment**

按 spec §4.3 顺序：GPU 曲线卡（1h/24h/7d 切换按钮 `data-win`）→ Token 用量卡（小时/天/月，prompt/completion 堆叠条，单 y 轴）→ 延迟 P50/P90 双线图（同单位同轴）→ 五联 KPI（KV Cache/Seq/TPOT/TTFT/Throughput，tooltip 文案沿用现有解释）→ 请求日志表 + 切换历史表（13px 紧凑）→ 费用卡。所有面板带 skeleton/empty state 容器。

- [ ] **Step 2: 重写 `js/monitor.js`**

`tabRenderers['tab-monitor']` 渲染 + 窗口切换事件委托 + ECharts 三图（GPU 显存/利用率双系列、Token 堆叠、P50/P90 两线；系列色取 `IFCharts.palettes[theme]` 顺序蓝→琥珀→绿→紫）+ 表格渲染（`UI.fmtDur`/`UI.fmtNum`）。数据源：`metrics_24h`（现 snapshot 已含 24h 窗口）+ `__TOKEN_STATS__` + `/api/token-curve`（粒度切换）。

- [ ] **Step 3: 验证**

pytest + `node --check` + 冒烟：`curl -s localhost:8999/ | grep -c monGpuChart`；浏览器确认监控页**无任何 button 型操作控件**（截图自检）。commit。

---

### Task 6: 推理 TAB（模型卡片 + 网关控制卡）

**Files:**
- Rewrite: `fragments/inference.html`、Create: `js/inference.js`

**Interfaces:**
- Consumes: `store`（`models sleep_states services_info services_health local_models`）、`UI`、`fetch('/api/metrics')`（cache hits）、`POST /admin/cache/toggle`（需 admin header，沿用现 `adminHeaders()` 逻辑移入 ui.js：`UI.adminHeaders()`）
- Produces: `tabRenderers['tab-inference']`；`window.doModelAction(name, action)`（action ∈ switch/stop/sleep/wake，switch 走现 overlay 机制 + `UI.confirm`）；网关控制卡 id `#gwCard`（含 `#cacheToggle` 开关 + `#cacheHits` 命中统计 + `#rlMeta` RPM/并发）

- [ ] **Step 1: 重写 fragment**

三组模型卡片（独占/共享/空闲，列头计数徽标）+ `#gwCard` 网关控制卡（LRU 缓存开关 + 命中 hits/total + RPM/并发元信息）+ 部署入口按钮（跳 tab-deploy）。每卡按钮：stop/sleep/wake（按状态可用性显隐），破坏性 `UI.confirm`。

- [ ] **Step 2: 写 `js/inference.js`**

`tabRenderers['tab-inference']`：按 `models` + `sleep_states` 分组渲染；`doModelAction` 调 `POST /switch|/stop|/sleep|/wake`（admin header）→ toast + `store.forceRefresh()`；`#cacheToggle` 点击调 `/admin/cache/toggle` 并回填；`/api/metrics` 读 cache hits 显示 `#cacheHits`；RPM/并发从 `/status` 的 rate limit 配置字段取（字段名以 `pm.mgr.status()` 实际返回为准，写代码前 grep `rate` 确认）。

- [ ] **Step 3: 验证**

pytest + `node --check` + 冒烟：`grep -c gwCard`；浏览器：切一个模型（overlay 出现→完成→卡片状态翻转）；toggle 缓存开关（API 200 + 状态翻转 + 命中数刷新）。**确认监控 TAB 中旧缓存控制已无残留**（spec §4.2：LRU 只出现在推理页）。commit。

---

### Task 7: 部署 TAB

**Files:**
- Rewrite: `fragments/deploy.html`、Create: `js/deploy.js`

**Interfaces:**
- Consumes: `POST /deploy`、`POST /pull`（admin header）、`UI.toast`、`UI.confirm`
- Produces: `tabRenderers['tab-deploy']`；表单 id `#vllmDeployForm`（字段沿用现 deploy.html：name/model_dir/port/gpu_mem slider）+ SGLang 卡；长任务进度态 `#deployProgress`；成功后 toast + `switchTab('tab-inference')`

- [ ] **Step 1-3:** 按上面实现；验证同 Task 6 模式（grep + 冒烟：填假表单点提交看 toast/进度态，不真部署）；commit。

---

### Task 8: 云端 TAB

**Files:**
- Rewrite: `fragments/cloud.html`、Create: `js/cloud.js`

**Interfaces:**
- Consumes: 现 app.js:30-270 的 cloud 函数族逻辑（`cloudLoadPresets/cloudAddPreset/cloudLoadProviders/cloudTestProvider/cloudDiscoverAll/cloudDiscoverOne/cloudDeleteProvider/cloudReload`，端点 `/admin/cloud/*`）
- Produces: `tabRenderers['tab-cloud']`；preset 网格 `#presetGrid` + 内联 key 表单 `#presetForm` + provider 表 `#provTable`（行内 test/discover/delete；delete 走 `UI.confirm` danger）

- [ ] **Step 1-3:** 把 app.js 的 cloud 函数族移植进 cloud.js（行为不变，换皮）；验证同前；commit。

---

### Task 9: 异常 TAB（事件表格化）

**Files:**
- Rewrite: `fragments/anomaly.html`、`js/anomaly.js`（覆盖旧文件）

**Interfaces:**
- Consumes: `GET /api/anomalies`（环缓冲事件）、`UI.icon`
- Produces: `tabRenderers['tab-anomaly']`；事件表 `#anomTable`（列：时间/severity 图标+文字/category/message），过滤 `#anomCatFilter #anomSevFilter` + 搜索 `#anomSearch`；critical 行 `row-crit` 类整行高亮；总计数 `#anomCount`

- [ ] **Step 1-3:** 实现 + 验证（过滤/搜索交互冒烟）；**同时删 app.js 中所有已迁移函数**（cloud 族 Task 8 已迁、anomaly 族本任务迁）；`node --check js/anomaly.js`；commit。

---

### Task 10: 删巨石 + 收口（empty state / 断连 banner / light 走查）

**Files:**
- Delete: `js/app.js`（所有函数已迁移至各 TAB 模块；删除前 grep 确认无残留引用）
- Modify: `base.html`（apiErrorBanner 绑定、`window.refreshNow` 指向 `store.forceRefresh`）、`__init__.py`（JS_MODULES 最终确认）
- Modify: `tests/unit/dashboard/test_dashboard_v2.py`（补全最终断言集）

**Interfaces:**
- 全局函数审计：`refreshNow/toggleTheme/switchTab/doSwitch/doReset/doReconcile/doReloadConfig` 全部在新模块中可解析（`node --check` + 冒烟 console 无 ReferenceError）

- [ ] **Step 1: 删 `js/app.js`**

```bash
grep -rn "app\.js\|loadModels\|loadOverview\|loadEngineMetrics\|loadLocalModels" inferfabric/dashboard/  # 必须 0 命中
rm inferfabric/dashboard/js/app.js
```
`git add -A inferfabric/dashboard/js/ && git commit -m "refactor(dashboard): remove legacy app.js monolith"`

- [ ] **Step 2: 断连 banner + skeleton 全量核对**

`store.on('api_error')` → 顶部 banner（`#apiErrorBanner` + "最后数据更新于 X"）；每个 TAB 空数据时 empty state 文案（动词开头，说明去哪操作）。

- [ ] **Step 3: light 主题走查（spec P5）**

逐页截图 dark/light 对比：遥测带、三张图表（主题重建）、表格、确认弹窗、toast；状态色对比度目测 ≥4.5:1（可用浏览器 devtools contrast 检查）；发现问题回改 style.css 令牌。

- [ ] **Step 4: 全量验证**

```bash
python3 -m pytest tests/unit/ tests/integration/ -q          # 全绿
python3 -c "import inferfabric"
IFF_OPERATIONAL_CONFIRM=1 python3 -m pytest tests/operational/ -q   # 有 GPU 环境则跑
# 运行时冒烟：/ 返回 200 且含 telemetryRail；/api/metrics 200；/v1/models 200
```

- [ ] **Step 5: 更新 `STATUS.md`（P1–P5 全 ✅ + 测试基线 + CVD 验证记录）→ commit**

---

### Task 11: 合入生产

按宪法：`git diff` review（LLM cross-review）→ 从 sandbox 合入 main → 生产环境全量 pytest + 启动冒烟（`python3 -m inferfabric.proxy.handler` + curl 三连）→ `STATUS.md` P9 Release 行 → commit。

---

## Self-Review（写完计划后自查记录）

1. **Spec 覆盖**：§2 双主题令牌→T1；§3 排版→T1；§4 布局/遥测带→T1/T2/T3；§4.3 监控纯遥测→T5；§4.2 LRU 归推理→T6；§5 交互→T1(confirm)/T10(断连)；§6 技术栈→T1/T4；§7 图表规则→T4/T5；§8 里程碑→T2(用户过目)/P5 走查→T10。无遗漏。
2. **Placeholder 扫描**：Task 7/8 的 "Step 1-3: 按上面实现" 有压缩嫌疑——执行时展开为与前任务同粒度的独立步骤（执行者按 T6 模板展开：重写 fragment → 写 js 模块 → 验证 → commit）。
3. **类型一致性**：`UI.*` / `store.*` / `IFCharts.*` / `tabRenderers` 签名在 T1 定义、各任务引用一致；DOM id 契约（`#telemetryRail` 子件、`#gwCard` 子件）在 Produces/Consumes 间对齐。
