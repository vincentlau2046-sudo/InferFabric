# Dashboard v2 — InferFabric Console（专业监控台重设计）

- 状态：设计已确认（2026-09-19），待实施
- 范围：`inferfabric/dashboard/` 全部（base.html、fragments/、js/、vendor/）+ 代理端 `_serve_dashboard` 无需改动
- 约束：不修改 proxy 转发核心路径（PR-14 约束）；后端 API 面不动；`get_html()` 拼装机制保留（`tests/unit/infra/test_core_comprehensive.py:1137` 依赖）

## 1. 产品定位与设计方向

InferFabric 是单 GPU 工作站的 AI 推理 OS。Dashboard 操作者是系统所有者本人。
视觉方向：**专业监控台**（Grafana 系），深色为主力主题、浅色为白天办公主题。
现有 macOS 拟物风（窗口圆点、拟物阴影、emoji 图标）全部移除。

## 2. 设计令牌（双主题）

令牌两层：primitive（色板原始值）→ semantic（`--bg`/`--surface`/`--ink`/状态色/强调色）。
`[data-theme="light"]` 仅覆盖 semantic 层，组件零改动。
主题切换：顶栏按钮（沿用 `#themeToggle`），持久化 localStorage，首访跟随 `prefers-color-scheme`。

### 2.1 Dark（主力）

| 令牌 | 值 | 用途 |
|---|---|---|
| `--bg` | `#0e1217` | 页面底色（深蓝灰，非纯黑） |
| `--surface` | `#161c24` | 卡片；**1px 边框 `#2a3340`，不用阴影** |
| `--ink` / `--ink-2` / `--ink-3` | `#e6eaf0` / `#9aa5b5` / `#66707f` | 三级文字 |
| `--accent` | `#d98e6e`（品牌铜） | 仅"当前活跃"状态 + 主操作，全局唯一强调色 |
| 状态色 | `ok #3fb950` `warn #d29922` `crit #f85149` `info #58a6ff` | 只表达状态，从不用作系列色；配图标+标签，不单独用色 |

### 2.2 Light

| 令牌 | 值 |
|---|---|
| `--bg` | `#f5f6f8` |
| `--surface` | `#ffffff`（边框 `#e2e5ea`，仍 1px、无阴影） |
| `--ink` / `--ink-2` / `--ink-3` | `#1a2029` / `#5b6572` / `#8a93a3` |
| `--accent` | `#b45a3c`（暗铜——亮底上浅铜对比度不足） |
| 状态色 | `ok #1a7f37` `warn #9a6700` `crit #cf222e` `info #0969da` |

两组状态色/系列色各自独立选定（不是自动反色），均须过对比度检查（状态色对各自 surface ≥ 4.5:1）。

### 2.3 图表系列色（两组，CVD 验证已通过）

固定顺序、不循环：蓝 → 琥珀 → 青 → 紫。
- dark 组（surface `#161c24`）：蓝 `#3b82f6` / 琥珀 `#b45309` / 青 `#0891b2` / 紫 `#7c3aed`
- light 组（surface `#ffffff`）：蓝 `#2563eb` / 琥珀 `#b45309` / 青 `#0891b2` / 紫 `#7c3aed`

>4 系列折叠为"其他"。
已用 `validate_palette.js` 验证：两组全 PASS，最差相邻 CVD ΔE 15.0（deutan，紫↔青），远超 ≥8 目标。
（原 spec 写"绿 `#3fb950`"，与琥珀 protan ΔE 仅 3.8 违反 §7，改青后通过——见 plan ledger 裁决。）

## 3. 排版

- 正文：系统 sans 栈（-apple-system, SF Pro, PingFang SC, Microsoft YaHei）；表格 13px / 正文 14px / 卡题 16px
- **所有数字等宽**：SF Mono / Menlo / Consolas + `font-variant-numeric: tabular-nums`（读数不跳动）
- 标签用 sans、句首大写（sentence case）；不用全大写标签、不用字距拉开的 eyebrow
- 零 webfont、零 CDN（机器离线可用）

## 4. 布局与主角元素

- 左侧 56px 图标导航栏（6 项 + 底部同步状态点）；主区 max-width 1280px
- 12 列网格，间距刻度 8 / 16 / 24px；所有对齐必须落在网格/刻度上
- **全局底部控制条取消**——操作下沉到所属 TAB
- **主角元素：GPU 遥测带（telemetry rail）**——总览页第一屏顶部一整条仪器条：
  VRAM（值/容量 + 填充条）· GPU 利用率 · 功耗 W · 温度 °C · 时钟 GHz。
  它是全界面唯一允许有视觉重量的元素；其余卡片同构、同密度、保持安静。

### 4.1 总览（tab-overview）

- 第一屏：GPU 遥测带（5 段）
- 第二行：活跃模型卡（名称 / engine / 端口 / uptime / 快捷操作 stop·sleep·wake，破坏性带确认）+ 24h 请求趋势 sparkline
- 第三行：最近异常 top 3（带"查看全部"入口）+ 系统操作卡（释放 GPU / Reconcile / 重载配置 / 强制重置——重置需确认弹窗）

### 4.2 推理（tab-inference）

- 模型卡片网格，按 独占 / 共享 / 空闲 三组；每卡：名称、engine 徽标、状态点、端口、uptime、操作按钮（stop/sleep/wake）
- **网关控制卡（本 PR 新增职责迁移）**：响应缓存 LRU（maxsize 500）开关（现位于本 TAB Gateway Status，语义上归此处）+ 命中统计（hits/total，取自 `/api/metrics`）+ rate limit 现状（RPM/并发，来自 `ratelimit` 配置）
- 部署入口按钮（跳转部署 TAB）

### 4.3 监控（tab-monitor，纯遥测、零操作）

所有控制类 UI 移出本 TAB。面板：

1. GPU 显存/利用率时间曲线（1h / 24h / 7d 切换，dataZoom 回放）
2. Token 用量：prompt/completion 堆叠条（小时 / 天 / 月）
3. 延迟 P50 / P90 双线（单轴；P50 与 P90 同单位可同图，禁止双 y 轴）
4. 五联 KPI：KV Cache / Seq Length / TPOT / TTFT / Throughput（保留现有指标语义与 tooltip 解释，换皮）
5. 请求日志表 + 切换历史表（表格化，13px 紧凑行高）
6. 费用概览卡（保留）

### 4.4 部署（tab-deploy）

- vLLM / SGLang 等部署表单卡片化（保留现有字段与 slider）
- 长任务（deploy / pull）显示进度态；成功后 toast + 自动跳转推理 TAB

### 4.5 云端（tab-cloud）

- 9 preset 网格 → 点击展开内联 API key 表单
- provider 表格：行内 test / discover / delete 操作；delete 带确认弹窗
- 手动配置表单卡（保留）

### 4.6 异常（tab-anomaly）

- 结构化事件**表格**替代现有 monospace 文本块：时间 / severity 图标 / category / message
- 过滤：category（routing/model/auth/config/cloud）+ severity（info/warning/error/critical）+ 全文搜索
- critical 行整行高亮

## 5. 交互与人性化细节

- 破坏性操作（reset / stop / gpu-clear / delete provider / release GPU）→ 确认 modal，写明后果
- 模型切换：全屏 overlay + 按钮互斥锁（沿用现机制），失败 toast 给原因
- API 断连：顶部 banner + "最后数据更新于 X"；每个 TAB 有 empty state 与 skeleton 加载态
- 数字统一格式化 helper（推广现有 `ovFormatUptime`：MB/GB、百分比、时长）
- 键盘可见 focus；尊重 `prefers-reduced-motion`；图表系列 ≥2 必须有 legend

## 6. 技术栈（已确认：方案 A 零构建）

- 仓库保持 Python-only：不引入 Node 工具链、不提交编译产物
- **ECharts** 单文件（`echarts.min.js`）vendor 到 `inferfabric/dashboard/vendor/` 并纳入 git
  （注意：`_deps/` 是 untracked，禁止放那里）
- 保留 `base.html` + `fragments/` + `js/` 拼装机制与 `get_html()` API；内容全部重写
- JS 模块化：`js/ui.js`（helpers/format/确认弹窗/skeleton）、`js/store.js`（现 state.js 数据层，
  `/api/snapshot` 轮询 + ETag 保留）、按 TAB 拆 `overview.js / inference.js / monitor.js / deploy.js / cloud.js / anomaly.js`，
  替代 1432 行 `app.js` 巨石
- 图标：内联 SVG symbol 集（现有 12 个基础上扩到 ~25：play/stop/pause/download/server/key/filter/trash/search…），
  全界面零 emoji
- 数据源（全部现有，不新增 API）：`/api/snapshot`（ETag）、`/api/anomalies`、`/api/token-curve`、
  `/api/engine_metrics`、`/api/metrics`、`/admin/cache/toggle`、`/history` 等

## 7. 图表规则（dataviz 技能约束）

- 先定图表形式再配色；单 y 轴，禁止 dual-axis
- 细线条（2px）、网格/坐标轴退隐、选择性直接标注（不给每个点标数）
- 状态色专用（good/warn/crit/info），不得充当"第 N 系列色"；状态必须图标+标签双编码
- 系列色固定顺序、颜色跟随实体不跟随排序
- 两组调色板（dark/light）用 `validate_palette.js` 跑 CVD 验证（目标 Delta E ≥ 8），FAIL 必须修正
- ECharts 实例按主题重建（不是换色滤镜）

## 8. 里程碑

| 里程碑 | 内容 | 验收 |
|---|---|---|
| P1 | 双层设计令牌 + 图标集 + Shell（顶栏/侧栏）+ GPU 遥测带 + 总览页 | **先交付纯静态 HTML 原型（假数据）给用户过目，点头再接数据** |
| P2 | ECharts vendor + 监控 TAB 全部时间序列（双调色板跑 CVD 验证） | 监控页只读、无操作按钮 |
| P3 | 推理 TAB：模型卡片 + 网关控制卡（LRU 迁移 + 命中统计 + rate limit） | 监控 TAB 零残留控制 |
| P4 | 部署 + 云端 + 异常 TAB | 事件表格化、确认弹窗齐备 |
| P5 | 收口：empty state / skeleton / 断连 banner / light 主题走查（遥测带、图表、表格、弹窗逐一截图对比） | 全量 pytest + 冒烟 |

每个里程碑在 sandbox 内独立可验收，最后统一 merge。

## 9. 治理流程（不变）

sandbox 开发 → 全量 pytest → `import inferfabric` 冒烟 → 运行时冒烟（核心端点 200）
→ diff review → LLM cross-review → merge。`_serve_dashboard` 路由、代理转发核心路径不碰。
`tests/unit/infra/test_core_comprehensive.py` 的 `get_html()` 断言必须继续通过。
