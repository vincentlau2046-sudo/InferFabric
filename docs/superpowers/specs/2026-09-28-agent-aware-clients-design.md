# Agent 感知（Client as Plugin）— 设计文档

- 日期：2026-09-28
- 状态：设计已达成（待用户终审）
- 范围：监控 TAB 客户端统计 + agents.d 自发现注册 + 一键认领落盘 + api_keys 规则文档

## 概述

把「客户端 Agent」做成与「模型」对称的**可配置插件资产**：每个 Agent 的识别特征声明在 YAML（`agents.d/`），代理按请求头每请求归类一次，同一份 `request_log` 加两列，监控 TAB 新增「客户端 / Agent 用量」卡。识别闭环最常触发的「认领未知 Agent」做成 **dashboard 一键落盘**，用户 ~90% 场景零手写 YAML。

设计继承 IFF 三条既有哲学：数据驱动 YAML + 热重载（同 `models.d` / `cloud_presets.yaml`）、统一遥测漏斗（`request_log`）、安全兜底优先（认不出 → unknown，不误伤）。

## 设计支柱

1. **agents.d 目录自发现** — 加 Agent = 加文件（内置 + 用户 drop-in 双目录）
2. **agent_registry.py 识别引擎** — `classify(protocol, headers) -> AgentHit`，每请求 O(1)
3. **监控统计** — `/api/agent-stats` 分桶聚合 + 监控 TAB 布局 A 卡
4. **一键认领闭环** — `POST /api/agents` 落盘用户目录 → 热重载 → 立即生效

## 1. agents.d 配置（自发现目录）

### 位置与优先级

| 目录 | 角色 | 归属 |
|---|---|---|
| `inferfabric/agents.d/*.yaml` | 出厂内置 matcher（claude-code / claude-desktop / cursor / codex） | git 管理 |
| `~/.inferfabric/agents.d/*.yaml` | 用户 drop-in 扩展（含一键认领落盘产物） | 用户租户，不进 git |

合并语义：
- 文件按**文件名排序**，跨文件 **first-match** 命中即归类
- **同 `id` 用户目录覆盖内置**（相似于 cloud_provider 覆盖 presets）；同目录内同 id 由文件名排序决定（首份生效）
- `id` 为 `unknown` 保留字，禁止用户定义
- 一键认领只写用户目录，永不写内置目录

### 配置格式（单 Agent 一个文件）

```yaml
# agents.d/curl-cli.yaml  —— 手写与一键落盘同格式
id: curl-cli          # 稳定键: ^[a-z0-9][a-z0-9-]{0,63}$（聚合/统计/key 命名用）
name: curl-cli        # 展示名（≤40 字符）
color: "#94a3b8"      # 图表分色
match:
  - { protocol: openai }
  - { header: user-agent, regex: "^curl" }
```

`match` 语义（逐条 OR，任一命中即归类）：
- `header` 白名单：`x-app` / `user-agent` / `x-client-version`；`value` 为**大小写不敏感全等**；`regex` 为 **`re.search`**（需锚点请显式写 `^`）
- `protocol` 可选：`anthropic`（`/v1/messages`）| `openai`（`/v1/chat/completions`、`/v1/generate`）；缺省 = 不限协议
- regex 编译失败 → **跳过该条并告警**，不影响加载（单条 fail-open）；整个文件解析失败 → **保留旧规则**（fail-closed，同 `auth.reload`）

### 内置初版 matcher

| id | name | match |
|---|---|---|
| claude-code | Claude Code | `x-app: cli`；`user-agent: ^claude-cli/` |
| claude-desktop | Claude Desktop | `x-app: desktop` |
| cursor | Cursor | `user-agent: Cursor\|Windsurf` |
| codex | Codex CLI | `user-agent: codex\|openai` |

## 2. agent_registry.py（识别引擎）

```python
@dataclass(frozen=True)
class AgentHit: agent: str; name: str; color: str; ua: str

class AgentRegistry:
    def load(self) -> None                # 初始加载: 合并 builtin+user → 不可变快照
    def reload(self) -> None              # 热重载（原子换快照指针，热路径不抢锁）; 失败保留旧快照
    def classify(self, protocol: str, headers: Mapping) -> AgentHit   # 未命中 → ("unknown","未识别","#9ca3af")
    def all(self) -> list[AgentDef]       # 图例/筛选
    def policy(self, agent_id: str) -> None | object   # 预留策略接缝，当前恒 None，不接线
    def add_from_ui(self, id, name, header, pattern, color) -> AgentDef  # 校验→落盘→reload
```

- 新模块 `inferfabric/agent_registry.py`（无第三方依赖；依赖 `yaml`，同 auth）
- 热重载挂入现有 `config_reloader`（SIGHUP / `/reload-config`），行为与 auth 一致（fail-closed）
- 快照为不可变对象，`classify` 单次引用获取，保证「每请求 O(1) + 热重载」不互相阻塞

## 3. 数据存储（迁移 v008）

- `RequestLog` dataclass 新增字段：`agent: str = ""`、`ua: str = ""`（命名与 `key_name/route` 同风格）
- `v008_add_agent_ua`：`request_log` 增列 `agent TEXT NOT NULL DEFAULT ''`、`ua TEXT NOT NULL DEFAULT ''`
- JSONL（`asdict`）自动带出新字段 → 学习回路原料
- 历史行 `agent=''` 与分类器规范值 `'unknown'` **在聚合层统一** `agent = agent or 'unknown'`（测试覆盖混存）

### 采集点

- 在两条协议请求入口（`chat_handlers.py` /v1/chat/completions 与 `handler.py` /v1/messages、/v1/generate，即现取 `auth_header` 处旁）各调用一次
  `handler._agent_hit = pm.agent_registry.classify(protocol(handler.path), dict(handler.headers))`
- 请求内所有 `RequestLog(...)` 构造点取 `handler._agent_hit` 传 `agent`/`ua`——**分类每请求只做一次**，防逐站遗漏/重复
- 非产生 RequestLog 的路径（静态资源、/api/* 等）不分类

## 4. 聚合 API

### GET /api/agent-stats?granularity=minute|hour|day|week&scope=all|local|cloud

复用 token-curve 分桶范式（Python 端分桶，SQLite 数据源；~2s flush 延迟同 token-curve）：

```json
{
  "granularity": "hour", "n": 24, "width_s": 3600, "window_requests": 192,
  "series": { "claude-code": [{"x":0,"requests":5}, ...] },
  "totals": [{
    "agent":"claude-code","name":"Claude Code","color":"#d97757",
    "requests":120,"success":118,"errors":2,"success_rate":98.3,
    "tokens_in":812345,"tokens_out":456789,"cost_yuan":0.0,
    "ttft_p50":312,"ttft_p95":890,
    "top_models":[{"model":"qwen38-27b-txt","requests":98}],
    "pct_of_requests":74.5, "source":"builtin"
  }],
  "unassigned": [{"ua":"curl/8.5.2","requests":12}]   // unknown 去重 UA top 20，每条截断 ~120 字符
}
```

- 费用：**抽共享 helper `cost_of_row(prices, row)`**（从 `_handle_token_curve` 现有逐请求计价逻辑提取），agent-stats 与 token-curve 同函数，杜绝口径漂移
- `scope=cloud` 时每桶 `cost` 为累计 ¥（本地恒 0）；`series` 稀疏语义（零请求 Agent 不出现）

### POST /api/agents（一键认领，admin-token 保护）

```json
{ "id": "curl-cli", "name": "curl-cli", "header": "user-agent", "pattern": "^curl", "color": "#94a3b8" }
```

- 服务端：校验（id 白名单 / name 非空 ≤40 / regex 可编译 / header 白名单）→ 模板生成 YAML → 临时文件 + rename 原子落盘 `~/.inferfabric/agents.d/<id>.yaml` → `registry.reload()`
- **`id` 已存在（内置或用户）→ 409**；保留字 `unknown` → 400；一键 = 新建语义，覆盖是手写文件特权
- 响应：`{agent: {...}}`；落盘文件头带 `# auto-generated ... (dashboard one-click)`
- 已知 Agent 从 `GET /api/agents` 获取：`{agents: [{id,name,color,source,match_rules}]}`

## 5. 监控 TAB 呈现（布局 A：col-12 单卡，图左表右）

插入 Token 用量行之后、模型延迟趋势之前；叙事链「用了多少 → 谁在用 → 快不快 → 明细」。

```
┌─ 客户端 / Agent 用量 — ●全部 ○本地 ○云端 ── [分钟|小时|天|周] ── ⚠未识别 3 ─┐
│ ┌─ 堆叠趋势(近24h·每1h) ──────────┐ ┌─ 明细(13px) ─────────────┐ │
│ │   ████ claude-code   ▇▇ cursor │ │ ● claude-code 120 98% ... │ │
│ │     ▄▄ python(未知)            │ │ ○ 未知 12 92% ... [识别]  │ │
│ └───────────────────────────────┘ └──────────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
```

- 图：堆叠（每色 = Agent，色取自 registry），y=请求数，tooltip 带 tokens/费用；`scope=云端` 时 y 轴切累计 ¥（复用 Token 双卡计量心智）
- 表：色点+名 | 请求数 | 成功率 | Tokens | 费用 | TTFT P50 | Top 模型；未识别行置顶、灰色、自带 `[识别]`
- 控制器：粒度 seg（分钟/小时/天/周，默认小时）+ scope seg（全部默认）；未识别 badge 常驻（0 时隐藏）
- 轮询：`/api/agent-stats` 30s 节流 + inflight guard + `isMonitorActive()` 门控；不随 3s snapshot 每轮打

### 一键认领交互

入口三处殊途同归（卡头 badge、明细表未识别行 `[识别]`、请求日志表 unknown 单元格 `✚`）：

```
┌─ 识别为新 Agent ─────────────────────┐
│ 样本: curl/8.5.2 · 近24h 12 次 mono │
│ 名称 [curl-cli]      来源 (◉)UA ( )x-app │
│ 规则 [^curl]  实时预览: 命中✓/不命中✗ │
│   [取消]              [创建并生效]    │
└──────────────────────────────────────┘
```

- 复用 `UI.confirm` 弹窗 + `UI.toast`；规则默认从样本 UA 生成（首个非字母数字前前缀，带 `^` 锚），**实时预览样本命中**，所见即命中
- 校验失败 → 字段内联红字；创建成功 → toast「已创建 Agent curl-cli，热重载已生效」
- **管理模式**（卡头「管理」按钮）：每行 `改名`/`删除`；删除只删用户目录文件，**内置 Agent 不可删**（误认可一键还原 → 信任感）

### 请求日志表改造

- `/api/request_log` 与 `/api/snapshot` 构造 rows 时**均新增 `agent`、`ua` 字段**（前两处都必须改，缺一处前端拿到 undefined）
- 表新增「客户端」列（时间后第 2 列）：Agent 色点+名；unknown 显示灰「未识别」+ UA tooltip + `✚`

### 人性化细节

| 场景 | 处理 |
|---|---|
| 无请求 | `if-empty`：「暂无客户端数据——经代理发起请求后在此统计」 |
| 未识别 >5 | 折叠为「展开全部 N」 |
| 主题 | Agent 色为固定 hex 仅作用于图/点，不踩 CSS var，亮暗可读 |
| 数字 | 复用 `fmtNum` / `_yuanFmt` / TTFT ms 口径 |
| 误删/换发 | 同名可重建；历史行 `agent=''` 归 unknown，不丢原始数据 |

## 6. api_keys.yaml 配置规则（仅文档，保持默认不启用）

规则写入 `auth.py` 模块 docstring（唯一权威）；行为不变（文件缺失 = 鉴权关闭 = 全放行记 anonymous）：

```yaml
# ═══ 命名规则（约定，不强制）═══
#   primary: sk-iff-<yyyymm>-v<n>          例: sk-iff-202609-v1
#   guest  : sk-<agent-id>-<yyyymm>-v<n>   例: sk-claude-code-202609-v1
#            对象取 agents.d 的 id → 将来可做 key_name ↔ agent 关联
#   yyyymm = 发卡年月；v<n> = 换发版本
primary: ""
guests: [ ]
```

本次不建文件、不开 CLI、不启用鉴权。

## 7. 安全边界

1. 一键落盘**只写 `~/.inferfabric/agents.d/`**，永不写内置目录；YAML 由模板生成非透传
2. `match` 字段白名单（3 header + 可选 protocol）；regex 必须可编译
3. `id` 白名单 `^[a-z0-9][a-z0-9-]{0,63}$` → 文件名安全，杜绝路径穿越；临时文件 + rename 原子写
4. `POST /api/agents` 走 control-plane admin-token 保护（同 /switch /stop，空 token = localhost-only）
5. 不修改代理转发核心路径（PR-14 划界不变）

## 8. 自洽性修正清单（本设计已内化）

- 聚合层 `agent = agent or 'unknown'` 归一（历史 `''` 与规范值混存不成两行）
- `/api/request_log` 与 `/api/snapshot` **两处**都要带 `agent`/`ua`
- 费用口径抽共享 `cost_of_row()`，禁止两处重复实现
- `regex` = `re.search`；`value` 大小写不敏感；`protocol` 作为可选 match 条件真参与匹配
- 一键 = 新建语义（409 防撞）；保留字 `unknown`
- 热路径取不可变快照，reload 原子换指针
- 分类每请求一次并缓存于 `handler._agent_hit`
- `unassigned` top 20 / 每条 120 字符；agent-stats 30s 节流

## 9. 测试计划

- **单测**：classify 规则矩阵（x-app 命中 / UA regex / 首击序 / protocol 条件 / unknown 兜底 / value 大小写 / 空目录全 unknown / 坏 regex 跳过 / 坏 yaml fail-closed）；`add_from_ui` 校验（id 白名单 / 保留字 / 409 / 原子落盘）；聚合（种子混存 `''`+`unknown` → 同一桶、费用 `cost_of_row` 口径）
- **集成**：v008 迁移（tmp db 升级 + 历史行默认）；`/api/agent-stats` 双协议种子验证；`POST /api/agents` 端到端（落盘 → reload → classify 命中）；`/api/request_log` 与 `/api/snapshot` 带新字段
- **沙箱冒烟**：dev 代理起服；curl 带 `x-app: cli` 与不带 → 明细分别显示 Claude Code / 原始 UA；一键认领 curl → 该 UA 下一请求归新 Agent
- 门禁：pytest 全绿 + `import inferfabric` 冒烟 + 交叉评审

## 10. 非目标与未来接缝

| 项 | 状态 |
|---|---|
| 认领时「归入现有 Agent / 合并」 | 未来（学习回路二阶：灰字自动建议 + 一键采纳） |
| `policy()` 接线（限流/白名单/切换差异化/KV 预热） | 未来，接口已留，恒 None |
| AnomalyEvent 带 agent / 按客户端过滤异常 | 未来（detail 已可容，微量改动） |
| agent × key 双维度下钻 | 配 guest keys 后解锁 |
| 专项「客户端」Tab | 策略面成熟后再议，监控卡 + 日志列已覆盖信息量 |

## 附录：关键决策记录

- 客户端主键 = **请求头特征指纹**（非 key、非 IP）：Agent 靠 header 自报家门，开不开鉴权都能区分
- 双文件承载（内置 + 用户目录）镜像 cloud_presets / cloud_provider 模式；一键落盘 = 用户目录普通 drop-in 文件
- 布局 A（col-12 图左表右）——用户 2026-09-28 拍板