# IFF 自动调度裁判（auto-routing judge）— 功能规格与设计文档

> 重大功能升级 · 设计阶段（P0 Intake → P2 Design）
> 状态：**DRAFT — 待用户审阅**
> 关联：iff tune（场景预设调优）、request_log_db、[tune-event]、prometheus 指标、GPU 状态机

## 1. 问题陈述

当前「切哪个模型 / 切哪个场景 / 何时切」完全由用户人工判断：
- 用户守着 Dashboard 指标（TTFT / 吞吐 / KV 水位）凭经验 `iff tune` 切档；
- 切换有 3-6s 停机代价 + 在途请求 503，用户不敢频繁试，导致负载模式与档位长期错配；
- 观测数据已齐（request_log_db、prometheus 指标、[tune-event] 场景变更事件），但**数据是死的**——没有一层机制把「指标 → 建议 → 决策 → 执行 → 复盘」串成闭环。

目标：加一层**自动调度裁判**——周期性读取性能统计，产出「该不该切、切到哪个场景/模型」的建议（最终形态可由 Laya 决策模型打分），经安全闸门后一键执行或低风险自动执行，全部留痕供复盘与后续模型训练。

## 2. 目标（Non-goals 明确划线）

### 2.1 做
| 能力 | 说明 |
|---|---|
| 观测窗口聚合 | 每 60s 把近 10min 指标聚合成标准化 `StateSnapshot`（判官输入） |
| **基线校准** | 每模型×每场景实测基线画像（baselines.yaml）；规则判断基于**相对基线的偏离系数**，不凭空定绝对阈值 |
| 规则裁判（RuleJudge） | 阈值 + 联合条件，先于一切产出建议（P0） |
| 建议与执行分离 | 裁判只产 `Recommendation`，执行永远走 `tune.apply` 既有安全路径 |
| 置信度闸门 | ≥90% 且低风险动作可自动；其余人确认（P0 即支持） |
| 建议留痕 | 每条建议（含被拒/被忽略）落结构化事件 = 后续 Laya 训练对来源 |
| Laya 决策模型（P1） | `laya-typed-decisions` 打分裁判，与规则裁判可切换/对拍 |

### 2.2 不做（本期明确排除）
- ❌ 不改变 GPU 状态机、switch/stop 语义、场景五不变量、模型 YAML 只读原则
- ❌ 不进入请求热路径：判官是旁路异步任务，任何情况下不阻塞请求转发
- ❌ 不做「全自动无人值守」开关默认开启——默认建议模式，auto 需显式开启
- ❌ 不碰 `tune.apply` 的写路径：判官无 tune 文件/锁/应用层写权，只有「调用入口」
- ❌ 不为本期引入模型评测体系（opt-in 时单独评估），Laya 接入是 P1 独立里程碑
- ❌ **不「拍脑袋定阈值」**：规则阈值必须经「基线采集 → 业务推导 → dry-run 验证」三步校准后才生效（见 §4.8）

## 3. 用户视角设计

### 3.1 形态
判官以「建议中心」出现，不做成侵略性自动机：

```
iff advise                          # 每 60s 评估一次，有建议才输出
┌─────────────────────────────────────────────┐
│ ⚙ 建议 · Qwen38-27B-TXT                     │
│   建议切换  big-batch → short-ctx            │
│   ──────────────────────────────────         │
│   置信度   88%（规则：TTFT p95 415ms > 300ms  │
│            × KV 水位 82% > 80% 联合触发）     │
│   代价预估  重启 ~4s · 在途请求短暂 503       │
│   [查看 diff] [执行] [忽略]                  │
└─────────────────────────────────────────────┘
```

Dashboard「⚙场景」卡片下方新增「建议」区：同构三态模态框（预览 diff→确认→执行），复用现有 tune 交互。

### 3.2 自动档
`iff advise --auto` 或 Dashboard「自动」开关开启后，仅满足以下**全部**条件才真正自动执行：
1. 置信度 ≥ 90%（规则裁判）或 ≥ 0.9（Laya 概率）；
2. 触发规则已完成基线校准并处于 `active`（pending/dry-run 规则不产生 auto 候选，见 §4.8）；
3. 动作属于低风险类（见 §4.6 Gate 表）；
4. 不在冷却期（同向切换后 10min 内不重复）；
5. 不在配置的「关键时段」（默认 22:00-00:00 生产窗口禁 auto，可配）。

不满足 → 降级为「建议 + 人确认」，绝不静默自动。

### 3.3 反悔与复盘
- `iff advise history`：与 `[tune-event]` 同格式的事件流，可回看「系统当时为何建议、是否被执行、结果如何」；
- 每条建议（含被拒/被忽略/超时未处理）都落日志——这是 P1 训练数据。

## 4. 技术设计

### 4.1 数据流（单向，判官永不写状态）

```
 request_log_db ─┐
 /metrics 环形缓冲 ─┼→ Observer(metrics_window) ─→ StateSnapshot
 [tune-event] ────┘          │
                             ↓
                    Judge 接口 (RuleJudge | LayaJudge)
                             ↓
                     Recommendation{action, target, confidence, reason, cost}
                             ↓
                   ConfidenceGate（置信度/风险/冷却/日历）
                    ┌────────┴────────┐
              建议模式              自动模式
         → Dashboard/CLI 人确认      → tune.apply(preview→restart)
                    └────────┬────────┘
                             ↓
                    [tune-recommend] 事件（含 outcome 回填）
```

### 4.2 模块布局（新增 `inferfabric/scheduler/`）

```
inferfabric/scheduler/
├── __init__.py        # 暴露 Scheduler，proxy 启动时挂载
├── observer.py        # StateSnapshot 聚合（读 request_log_db + metrics + tune-event）
├── judge.py           # Judge 协议 + RuleJudge（P0）+ LayaJudge 适配器（P1）
├── gate.py            # ConfidenceGate / 风险分级 / 冷却 / 日历
├── recommend_event.py # [tune-recommend] 事件发射（logging，同 [tune-event] 风格）
└── loop.py            # asyncio 周期任务（60s），判官调度入口
```

依赖方向：`scheduler → tune（只调 apply/preview 接口） → config/engine_adapter`。**无反向依赖**；scheduler 不 import 任何引擎实现细节（沿用 tune 的引擎无感知原则）。

### 4.3 核心数据模型

**StateSnapshot**（判官输入，纯快照无副作用）：
```python
@dataclass(frozen=True)
class StateSnapshot:
    ts: float                 # 窗口结束时间
    model: str                # 模型名（判官按模型分别评估）
    window_s: int             # 聚合窗口（默认 600）
    # 延迟/负载（来自 request_log_db + metrics）
    ttft_p50_ms: float; ttft_p95_ms: float
    tpot_mean_ms: float
    throughput_rps: float
    error_rate: float
    running_batch_max: int
    kv_cache_usage_pct: float   # 池占用%（指标，非超卖%）
    queue_depth: int
    # 场景上下文（来自当前 live 值 + 场景定义）
    current_preset: str         # "" = default
    params: dict                # 当前 C/W/draft 等
    pool_top: int; oversell_pct: float; kv_capacity: int
```

**Recommendation**（判官输出，纯建议）：
```python
@dataclass(frozen=True)
class Recommendation:
    ts: float
    model: str
    judge: str                # "rule" | "laya"
    action: str               # "stay" | "switch_preset" | "switch_model"
    target: str               # preset 名 / 模型名；action=stay 时 ""
    direction: str            # "scale_down" | "scale_up" | "same" | ...
    confidence: float         # 0-1
    reason: str               # 人类可读，含触发指标
    cost_estimate_s: float    # 预计切换停机秒数（重启 ~3-6s）
    auto_eligible: bool       # 是否通过 Gate 的低风险判定
```

### 4.4 RuleJudge（P0 具体规则，可配）

**核心原则：规则判定基于「相对基线的偏离系数」，不基于凭空定的绝对阈值。**

两者分工：
- **baselines.yaml**（§4.8 校准产物）：每模型×每场景的稳态画像（TTFT/KV/吞吐等）——「正常」的定义；
- **rules.yaml** 规则只写「与基线相比偏离多少算异常」的**系数/差值**（k 值），业务语义由校准步骤推导（§4.8 步骤 2），不在规则里硬编码毫秒/百分比。

规则放进 `models.d/rules.yaml`（侧车，同 scenarios.yaml 哲学：git 审、可调）：

```yaml
Qwen38-27B-TXT:
  check_interval_s: 60
  window_s: 600
  key_hours: ["09:00-22:00"]        # 关键时段外才允许 auto（业务可调）
  rules:
    - id: ttft-high-scale-down
      # TTFT p95 连续 2 窗口超基线 ×2.0，且 KV 水位超基线 +15pct → 降档（short-ctx）
      when:
        ttft_p95_ms: {relative: ">", k: 2.0, windows: 2}
        kv_cache_usage_pct: {relative: ">", k: 1.0, delta_pct: 15}
      action: switch_preset
      target: short-ctx
      direction: scale_down
      confidence: 0.88            # 该校准步骤推导的「人工会怎么切」编码置信度
      note: 低延迟档缓解排队
    - id: kv-full-scale-down
      # KV 绝对水位超 90%（硬保护，唯一允许的绝对阈值兜底）→ 降档
      when: {kv_cache_usage_pct: {gt: 90, windows: 2}}
      action: switch_preset
      target: short-ctx
      direction: scale_down
      confidence: 0.92
    - id: idle-reset-suspicious
      # 接近空闲 + 错误率异常偏高（相对基线 +2σ）→ 回 default 复位
      when:
        error_rate: {relative: ">", sigma: 2.0}
        throughput_rps: {lt: 0.5}
      action: switch_preset
      target: default
      direction: reset
      confidence: 0.85
    - id: underloaded-scale-up
      # 长时间显著低于基线（TTFT < 基线×0.5 ×3窗口 × KV < 基线−20pct）→ 升长窗档
      when:
        ttft_p95_ms: {relative: "<", k: 0.5, windows: 3}
        kv_cache_usage_pct: {relative: "<", k: 1.0, delta_pct: -20}
      action: switch_preset
      target: small-batch
      direction: scale_up
      confidence: 0.7
```

**条件算子**（`when` 表达式）：
| 算子 | 语义 | 用途 |
|---|---|---|
| `{relative: ">", k: 2.0}` | 观测值 > 基线值 × k | 均值类指标（ttft/tpot）的偏离 |
| `{relative: ">", k: 1.0, delta_pct: 15}` | 观测值 > 基线值 × k + delta_pct 个百分点 | 百分比指标（kv 水位）的偏离 |
| `{relative: ">", sigma: 2.0}` | 观测值超出基线分布均值 +2 标准差 | 错误率等方差敏感指标 |
| `{gt: 90}` / `{lt: 0.5}` | 绝对阈值（**仅限兜底硬保护**） | KV 满池 / 空闲判定 |
| `windows: N` | 连续 N 个窗口均满足才触发 | 防抖 |

**约束：`relative:` 算子依赖该模型×该场景的基线；基线缺失 → 该规则跳过（不产出该方向建议），并记日志提示先跑 `iff calibrate`**（§4.8 步骤 1）。

规则属性：每个规则自带 `confidence`，是对「人工确认此种情况会怎么切」的编码——**规则集本身就是 Laya 的监督标签来源**。

### 4.5 LayaJudge（P1，独立里程碑，本期仅留适配器骨架）

- 依赖：vendor `laya` + `transformers` 进 `_deps`（新增，不影响现 vendored 依赖）
- 形态：`pip install laya` 的 `laya-typed-decisions`，输入 state+questions 单次前向
- 输入：StateSnapshot 压缩为 JSON（≤1024 token）+ questions：
  ```
  state: {model, window, ttft_p95, kv_pct, error_rate, current_preset, params...}
  questions:
    action:     {type: choice, options: {short-ctx: 低延迟...,
                 small-batch: 顶窗..., big-batch: 长窗批处理...,
                 default: 基线, stay: 保持不变}}
    confidence: {type: noul, instructions: 当前是否应立即切换}
  ```
- 决策：`choice` 取 max 概率；act-vs-escalate gate 的 `noul` 概率做置信度闸门输入
- 对拍：与 RuleJudge 同窗口并行评估，记录双方建议与最终 outcome，**Laya 仅在 held-out 上验证优于规则召回后才成为主裁**；否则停留规则主裁 + Laya 建议标注
- 加载策略：首次使用延迟加载（懒加载，避免常驻占用）；加载失败/超时 → 静默回退 RuleJudge
- 运行位置：判官本就在旁路线程/线程池 → Laya 推理（421M）放 `asyncio.to_thread`，不阻塞判官主循环

### 4.6 Gate（安全闸门）

| 检查 | 参数 | 默认 | 说明 |
|---|---|---|---|
| 置信度 | `min_confidence` | 0.9（auto）/ 0.0（建议模式） | 建议模式任何置信度都展示；auto 需 ≥0.9 |
| 风险分级 | 动作分类 | 见下 | 仅低风险可 auto |
| 冷却 | `cooldown_s` | 600 | 同模型同向切换后冷却，防抖 |
| 日历 | `key_hours` | 09:00-22:00 | 关键时段内禁 auto，仅建议 |
| 模型活性 | model 需已加载 | — | 休眠模型不触发（先醒/切由人决定） |

风险分级（判定 `auto_eligible`）：
- 低风险（可 auto）：保持 `default` / 降并发档 / 缩窗档（scale_down 向短窗/低 C 移动——切坏了可以再切回去，停机已发生但方向保守）
- 高风险（仅建议）：升并发/长窗档（scale_up——可能把 KV 撑爆）、跨模型切换（vLLM/NInfer 之间）、`switch_model`

### 4.7 事件（[tune-recommend]）

与 [tune-event] 同风格（单行 JSON，logging INFO，CLI→stdout，Dashboard→journal）：

```json
{"event":"tune.recommend","ts":1727...,
 "model":"Qwen38-27B-TXT","judge":"rule","action":"switch_preset",
 "from_preset":"big-batch","to_preset":"short-ctx","direction":"scale_down",
 "confidence":0.88,"reason":"TTFT p95 415ms>300ms × KV 82%>80%",
 "cost_estimate_s":4,"gate":"auto","outcome":"accepted|rejected|ignored|executed"}
```

`outcome` 回填规则：
- 建议模式：用户点击执行 → `executed`；点忽略 → `rejected`；超时（建议过期）→ `ignored`
- 自动模式：执行成功 → `executed`（随后 [tune-event] 记录 apply 结果）；失败回滚 → `rolled_back`

### 4.8 基线校准（Baseline Calibration）——规则生效前的正式确定环节

**为什么需要这一节**：规则的 `relative` 系数（k、delta_pct、sigma）和兜底绝对阈值若无依据，等于把「拍脑袋」从阈值本身挪到了系数上。因此设定一个**三步闭环**——基线采集 → 业务推导 → dry-run 验证——只有全部通过后规则才「生效（active）」；在此之前相关规则**被标记为 `pending`，只记录不触发动作**。

**步骤 1 · 基线采集（`iff calibrate`）**

- 输入：近 N 天（默认 7 天）的 request_log_db + prometheus 指标，按 **模型 × 场景** 分组；
- 输出：`models.d/baselines.yaml`（git 可审、可人工复核后 git commit）：
  ```yaml
  Qwen38-27B-TXT:
    short-ctx:
      window_s: 600
      samples: 1008            # 近 7 天 10min 窗口数（有流量）
      ttft_p95_ms:  {mean: 120, std: 35}
      ttft_p50_ms:  {mean: 60,  std: 18}
      tpot_mean_ms: {mean: 42,  std: 9}
      kv_cache_usage_pct: {mean: 55, std: 12}
      throughput_rps: {mean: 2.1, std: 0.8}
      error_rate:    {mean: 0.004, std: 0.012}
    default:  ...
    small-batch: ...
    big-batch:  ...
  ```
- 资格判定：该模型×场景窗口数 ≥ `min_samples`（默认 200，约 2 天流量）才写入基线；不足 → 该格留空并在日志标注「样本不足，待采集」，对应规则保持 pending。
- 重采集触发：模型配置/场景定义/流量基线结构变化（kv_capacity 调整等）→ 重新校准；更新时保留旧版本于 git 历史。

**步骤 2 · 业务推导（系数从业务 SLA 出发，不凭空定）**

把「用户业务要什么」翻译成 `relative` 算子参数。每一项必须回答「这条规则判断的业务现象是什么」：

| 业务目标（示例） | 推导出的规则参数 | 依据链 |
|---|---|---|
| 交互/agent 请求 TTFT 目标 < 300ms（P95），超时不爽 | `ttft_p95_ms: {relative: ">", k: 2.0, windows: 2}` | 基线上慢档 TTFT p95≈150ms；2×=300ms 恰为 SLA 红线，2 窗口防抖确认非偶发尖峰 |
| KV 池剩余须能容纳一个典型长请求（≈10K tokens） | `kv_cache_usage_pct: {relative: ">", k: 1.0, delta_pct: 15}` | 基线水位 55%，+15pct→70% 剩余 30%≈188K tokens，仍足够；继续升到 90%（兜底 `{gt: 90}`）代表物理满池 |
| 错误率异常 = 引擎/配置退化信号 | `error_rate: {relative: ">", sigma: 2.0}` | 基线 error 0.004±0.012（长尾），+2σ 抓到罕见退化但避开正常抖动 |
| 明显低负载可整合到长窗档 | `ttft_p95_ms: {relative: "<", k: 0.5, windows: 3}` + `kv < 基线−20pct` | TTFT 掉到一半 + L3 窗口，说明排队与 KV 压力远低于档位容量，可安全降 C 升窗 |

- 每个参数在 rules.yaml 里必须带 `note:` 写明「业务依据」；无业务依据的规则不许进入 dry-run。
- 兜底绝对阈值（`{gt: 90}` / `{lt: 0.5}`）仅限两类：物理资源硬保护、空闲判定，**其余一律走 relative**。

**步骤 3 · dry-run 验证（`iff calibrate --dry-run N`，N 默认 7 天）**

dry-run 期间的语义——**判官照常跑、照常产 Recommendation、照常落 [tune-recommend]（outcome=`dry_run`），但绝不触发执行（建议模式与 auto 模式都不执行）**：

1. **回放验证**：用最近 N 天历史数据回放每条规则，产出「假如当时执行会怎么切」的决策日志；
2. **人工对比**：将 dry-run 决策与**当时人工实际做的 tune 动作**对齐——云台看板或 `iff advise history` 展示「本规则建议 vs 当时人工选择」，人工确认逻辑合理；
3. **评估指标**：统计本轮 dry-run 的 `precision@K`（建议被人工采纳/认可的比例）、误报数、方向合理性抽查；业务方对每条规则给出「通过 / 调整参数 / 废弃」结论；
4. **切换判定**：rule 状态机 `pending → active`（通过）/ `pending → disabled + note`（废弃）；active 规则才计入 auto 资格与置信度闸门；
5. 默认周期：dry-run 观察 ≥7 天（真实流量），复杂规则/高风险方向（scale_up）可延长至 ≥14 天。

**校准产物即文档**：baselines.yaml + rules.yaml(active/pending 标注) + `iff advise rules --dry-run-report` 汇总页，三者共同构成「当前判官为何这么判」的可审依据。**M2 验收以「至少一条规则完成三步校准转 active」为前提**（见 §10）。

### 5.1 新增 CLI
```
iff advise [model]             # 当前建议（无建议则 "保持当前配置"）
iff advise --auto             # 开启/执行自动模式（需确认）
iff advise history [model]    # 建议事件流（同 tune-event 格式）
iff advise rules              # 展示当前 rules.yaml（含 active/pending/dry-run 标注）
iff calibrate [model]         # 基线采集 + 业务推导，产出/更新 baselines.yaml
iff calibrate --dry-run N     # 最近 N 天回放验证：产 dry-run 报告，不触发任何执行
```

### 5.2 Dashboard
- 「⚙场景」卡片新增「建议」区块：显示最新 Recommendation（conf/理由/代价/方向），按钮「查看 diff / 执行 / 忽略」；
- 执行按钮复用现有 tune 三阶段模态框（preview diff → confirm → restart 状态机）；
- 「自动」开关（默认关），开启时显示当前 Gate 状态（置信度/冷却/关键时段剩余）。

## 6. 非功能要求

| 要求 | 指标 |
|---|---|
| 判官开销 | 评估循环本身 <100ms（不含 Laya 推理）；Laya 推理走线程池 ≤1s |
| 无侵入 | 永不进入请求热路径；判官崩溃/挂死只落 error，不影响服务 |
| 可观测 | 每条建议/每次执行有事件；判官自身有 `/api/scheduler` 状态端点 |
| 可回滚 | 沿用 tune.apply 回滚机制；自动执行失败与手动失败同路径 |
| 幂等 | 同一窗口重复调用判官产出相同 Recommendation（纯函数化） |

## 7. 测试计划（新增 tests/unit/scheduler/）

| 测试文件 | 覆盖 |
|---|---|
| test_observer.py | StateSnapshot 聚合正确性（合成 request_log_db/metrics 数据）、窗口滑动、缺数据容错 |
| test_rules.py | 每条规则触发/不触发矩阵；联合条件与 `windows` 计数；confidence 随规则 |
| test_gate.py | 置信度/冷却/日历/风险分级各分支；auto 与建议模式切换 |
| test_loop.py | 60s 调度、幂等性、判官异常不冒泡 |
| test_recommend_event.py | 事件 schema + outcome 回填矩阵（含 `dry_run`） |
| test_calibrate.py | 三步校准闭环：基线统计正确性、min_samples 资格判定、业务推导参数落库、dry-run 回放与人工决策对齐、pending→active 状态机、绝对阈值仅限兜底 | 
| test_laya_stub.py | P1 适配器骨架：懒加载、失败回退 RuleJudge、线程池不阻塞（stub 模型） |

集成：`tests/integration/test_scheduler_loop.py`（真实 request_log_db + 假 metrics + FakeMgr，走完整 建议→Gate→tune.apply 链，复用现有 test_tune.py 的 FakeMgr 模式）。

## 8. 上线顺序（里程碑）

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M0 设计审阅**（本次） | 本 spec 审阅通过 | 用户拍板 |
| **M1 骨架** | scheduler/ 空模块 + StateSnapshot + Recommendation 定义 + observer 聚合 | observer 单测绿；`iff advise` 输出「保持」 |
| **M2 基线校准** | `iff calibrate`（采集→业务推导→dry-run 回放）产出 baselines.yaml；RuleJudge 按 pending 接入 | 校准单测绿；≥1 条规则 dry-run 验证通过转 active（§10 验收 2-3） |
| **M3 RuleJudge + Gate** | rules.yaml（active 规则）+ gate + [tune-recommend] 事件 + CLI/Dashboard 建议展示 | 规则测试绿；真实数据跑通建议；auto 候选仅限 active 规则 |
| **M4 执行接线** | 建议模式「查看 diff→执行」接入 tune.apply；auto 模式 + 冷却/日历 | 集成测试绿；人工验收一轮 |
| **M5 数据积累** | 建议历史事件 ≥2 周 | outcome 数据可导出为训练对 |
| **M6 Laya**（独立） | vendor laya；LayaJudge 适配器 + 对拍 | held-out 对比表，达标才主裁 |

## 9. 风险 + 缓解

| 风险 | 概率/影响 | 缓解 |
|---|---|---|
| 规则误判导致频繁切换 | 中/中 | 冷却期 + 方向保守 + 建议模式默认 + 事件留痕可复盘 |
| auto 模式切坏现场 | 低/高 | 仅低风险动作可 auto；沿用回滚；关键时段禁 auto |
| Laya 决策质量不达标（0.766 通用基准 ≠ 本场景） | 中/中 | 数据积累 + held-out 对拍后才主裁；不达标则保持规则主裁 |
| _deps 增加 transformers 体积/兼容 | 低/中 | 懒加载；P1 才引入；不影响现 vendored 启动 |
| 判官与手动 tune 并发（双写应用层） | 低/高 | 判官不碰写路径，只调 apply（文件锁内）；手动优先（apply 锁互斥） |

## 10. 验收标准（M3 完成时判定）

1. `iff advise` 在合成高延迟数据（基于基线构造的相对偏离，非绝对阈值）下输出 short-ctx 建议（置信度/理由/代价齐全）；
2. 三步校准闭环可跑通：`iff calibrate` 产出 baselines.yaml → 业务推导参数 → dry-run 回放生成「建议 vs 当时人工决策」对齐报告；
3. dry-run 期间建议只记录不执行（outcome=`dry_run`），auto 模式被抑制；规则 `pending → active` 必须经过已完成的 dry-run 观察期；
4. 自动模式：满足 Gate 全部条件时执行成功并落 [tune-recommend]/[tune-event]；
5. 任一 Gate 不满足 → 绝不自动执行（测试覆盖矩阵）；
6. 判官崩溃不影响 proxy 请求转发（故障注入测试）；
7. 全量 pytest 通过（新 + 既有）。

## 11. 关联资产（不修改）

- models.d/*.yaml：只读真源原则不变；rules.yaml 是**新增**侧车，不并入模型 YAML
- tune.py 五不变量（D1-D5 + P0-4）：判官不引入新写路径，天然继承
- GPU 状态机 / Switch Guard / 引擎适配器：零改动