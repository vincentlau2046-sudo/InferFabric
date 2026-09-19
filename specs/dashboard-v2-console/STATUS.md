# Dashboard v2 Console 重设计 状态

| Phase | 状态 | 日期 | 备注 |
|-------|------|------|------|
| 设计 | ✅ | 2026-09-19 | 用户确认：专业监控台方向 / 方案 A 零构建（vanilla + ECharts vendor）/ 双主题（dark 主力 + light）/ 六 TAB 全重做 / LRU 等网关控制归推理 TAB、监控 TAB 纯遥测 |
| spec | ✅ | 2026-09-19 | spec.md |
| P1 实施 | ✅ | 2026-09-20 | 设计令牌（双主题 semantic 层）+ Shell（56px 图标导航 + 顶栏）+ GPU 遥测带（telemetryRail 五段仪器条）+ 总览 TAB（Task 1-3） |
| P2 实施 | ✅ | 2026-09-20 | ECharts vendor（echarts.min.js 内联）+ 监控 TAB 七面板（双调色板 CVD 验证通过，见下）（Task 4-5） |
| P3 实施 | ✅ | 2026-09-20 | 推理 TAB 三组模型卡 + 网关控制卡（LRU 缓存开关 + rate limit 现状 + 部署入口）（Task 6） |
| P4 实施 | ✅ | 2026-09-20 | 部署（表单卡 + 长任务进度态）/ 云端（9 预设网格 + Provider 表 + 手动配置 + 发现模型）/ 异常（结构化事件表 + 过滤 + critical 高亮）（Task 7-9） |
| P5 收口 | ✅ | 2026-09-20 | app.js 巨石删除（1432 行死代码，零引用门通过）/ skeleton + empty state 全 TAB 标准化（cloud/anomaly 加 UI.skeleton，deploy 静态表单豁免）/ 断连 banner 补"最后数据更新于 X"（store.js api_error handler）/ 全量 pytest 799 绿 + 运行时冒烟（:18999 三端点 200）/ light 主题程序化验证（headless 计算样式 + 令牌核对，见下） |

## 测试基线

- P5 收口（2026-09-20）：全量 pytest `tests/unit/ + tests/integration/` **799 passed**（`python3 -m pytest tests/unit/ tests/integration/ -q`，27s）
- 运行时冒烟（2026-09-20，:18999 隔离端口）：`/` 200（含 telemetryRail + 全部 6 个 tabRenderers，0 处 app.js 引用）/ `/api/metrics` 200 / `/v1/models` 200
- Dashboard 单测：`tests/unit/dashboard/test_dashboard_v2.py` 54 passed（含 Task 10 新增 5 项：app.js 删除 / 零引用 / 6 tabRenderers 装配 / skeleton 标准化 / 断连 banner 时间戳）

## CVD 验证记录

- 双调色板 CVD 验证 **PASS**（ΔE 15.0，worst-adjacent deutan 紫↔青，远超 ≥8 目标；8 系列色 dark/light 两组，见 `task-4-palette.js`/`validate_palette.js` 记录）。
- 状态色对比度核对（P5，程序化）：8 组状态色 × 各自 surface 全部 ≥ 4.5:1（dark 5.11–6.79，light 4.87–5.36）。

## Light 主题走查（P5）

- 程序化验证（headless Chrome CDP，`get_html()` 全内联页面）：
  - `[data-theme="light"]` 令牌块完整（--bg/--surface/--ink 三级/--accent/状态色 4 色/--border + skeleton/overlay/series）；
  - 关键元素计算样式 dark↔light 对照通过（body/telemetryRail/.if-card/.if-table/.if-modal/#apiErrorBanner 均随主题切换，组件零改动，与 spec §2.2 数值逐一对上）；
  - 组件 CSS 无硬编码 hex（除 4 处 accent 面上的 `#fff`/`#000` 常量——`::selection`、`.btn-pri` 文本、`.btn-pri:hover` color-mix、`.cp-preset.selected .cp-mono`——非主题 bug，列见下方观察项）。
- **观察项（留待人工复核）**：dark 主题 `#fff` 文本 on `--accent #d98e6e` 对比度 ≈ 2.61:1（低于 WCAG AA 4.5:1；light 主题 `#b45a3c` ≈ 4.70:1 通过）。accent 为 spec §2.1 定稿值，改动超出 P5 范围。
- **visual eyeball light/dark 走查：pending human review**（无头环境无法目测，R19 裁决以程序化验证替代）。

## 模块结构（收口后）

- legacy `js/app.js`（1432 行）已删除；JS 模块化为 9 个模块：`ui / store / charts / overview / inference / monitor / deploy / cloud / anomaly`（`__init__.py` JS_MODULES 显式列表，无 app）。
- store.js 自举（restoreTab + startPolling）；全局 `refreshNow/toggleTheme/switchTab` 均由 store.js 定义。
- 历史文档（`steering/bounded-context.md` v4.6.5、`specs/v47-cloud-presets/`）中的 app.js 提及为版本化记录，保留不改。
