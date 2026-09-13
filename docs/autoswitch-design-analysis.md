# InferFabric Auto-Switch 模型切换设计深度分析

> 日期：2026-09-10
> 范围：代理请求驱动的模型自动切换（`EDGE_AUTO_SWITCH`）、模型切换生命周期、VRAM 预算
> 状态：分析 + 方案（待实施）

## 0. 功能摘要与预期效果（TL;DR）

本方案要解决的问题：`EDGE_AUTO_SWITCH=1` 下，任何请求命中"已知但未激活"模型即触发切换，
导致杂散请求可能挤掉活动模型（§3.2）。实施后的行为变化：

| 场景 | 优化后行为 | 达到效果 |
|---|---|---|
| 探针/健康检查等不带头请求 | 不触发切换：路由到当前活动模型，无活动模型则 503 + Retry-After | 消除"杂散请求挤出活动模型"风险；qwen38-27b 不再被探针挤掉 |
| 带 `X-IFF-AUTO-SWITCH: true` 的请求 | 触发 `ensure_service` → 独占切换 | 只有显式声明要切换的调用方才切换，意图明确 |
| 旧模型上有在途生成任务（措施 B） | 停旧模型前 `POST /pause?mode=wait` 等在途请求跑完（超时后降级 `mode=abort`） | 在途生成不被硬停打断，agent 长任务不中断 |
| 同一模型停用后重新激活（措施 C） | L1 睡眠（权重驻留显存，仅释放 KV cache），`POST /wake_up` 亚秒级唤醒 | 同模型再激活从 ~540s 冷启动降为亚秒级 |
| 小模型 + 大模型共存 | L1 不释放权重，显存占用不变；bge-m3（gpu_role=none, CPU）与 GPU 状态机解耦可共存 | 小模型不受 GPU 切换影响 |
| 两个大模型 exclusive→exclusive 互换 | 仍是"停旧→启新"（VRAM 硬约束） | 行为不变，但排水保证无中断 |

预期整体效果：自动切换由"任意请求均可触发"收敛为"显式 opt-in 才触发"；活动模型更稳定、
在途任务不再被硬停打断、同模型再激活亚秒级完成。

## 1. 背景

- 2026-09-09 已在 systemd 用户单元 `~/.config/systemd/user/inferfabric-proxy.service` 中将
  `Environment=EDGE_AUTO_SWITCH=0` 改为 `=1` 并重启验证。代理（127.0.0.1:8999）现在对
  "已知但未激活的模型"请求会触发 `ensure_service`（`proxy/handler.py` L336-361）。
- 本地环境：单卡 32GB（32607 MiB），当前独占模型 `qwen38-27b-abliterated`（vLLM 独占，~17GB）；
  `bge-m3` 为 `gpu_role=none` 独立服务（`_switch_independent`，与 GPU 三态状态机解耦，可共存）。

## 2. 源头更正："shared → shared 全量重启" 是 V1 过时说法

**错误认知的源头**：`manager.py` 中三处 V1 时代的陈旧注释/docstring（均已修正）：

| 位置 | 原文 |
|---|---|
| `ModelManager` 类 docstring（L53） | `add/remove shared service (hot-plug V1: full restart)` |
| `switch()` docstring（L274） | `shared → shared: allowed (add/remove service, V1: full restart)` |
| `switch()` 内联注释（L388） | `# V1: full restart — stop all, then start all including new one` |

**权威信息源应为当前实现**（`manager.py` L387-389 的调用链）：

```
current_mode == SHARED and target_mode == SHARED
  → self._lifecycle._shared_add_service(model)   # model_lifecycle.py L201
```

- **增量添加**（`_shared_add_service`，L201-270）：只启动新模型，启动前做 97% VRAM 预算检查
  （`current_vram + model.peak_vram_mb > 0.97 × gpu_total` 则拒绝），现有 shared 服务**继续运行**，
  不中断活动任务。
- **定点移除**（`stop_service`，L498-556）：只停指定服务；停完用 `_wait_gpu_idle(timeout=20)`
  校验显存确实释放，未释放则 `_pkill_by_port(该模型端口)` 定点强杀；若是最后一个 shared 服务，
  自动回落 idle。

**结论**：shared→shared 不是全量重启，是增量热插拔。三处陈旧注释已更新为增量表述。

## 3. 模型切换设计深度分析

### 3.1 shared → shared 全量重启是否必要？

**不必要，且当前实现未做全量重启**。依据：

- vLLM 单进程不能同时服务多个独立模型（vLLM 论坛确认 + vLLM issue #13633）→ 每个模型独立进程 +
  代理按 model 名路由（`proxy/handler.py` PR-2a/2b），这是正确架构。
- VRAM 预算按整个池子计算，但增删是单服务粒度：添加前查预算、移除后验释放。

### 3.2 独占模式下的自动切换风险（杂散请求挤出活动模型）

`EDGE_AUTO_SWITCH=1` 后，任何 `/v1/messages` 请求若命中"已知但未激活"的模型，就会触发
`ensure_service` → `_switch_exclusive`（`model_lifecycle.py` L286）：硬停旧 vLLM 进程 + 清 CUDA
状态 + 冷部署新模型（失败回滚旧模型）。

**风险**：一条杂散的存活探针、健康检查，或新 agent 使用默认模型的请求，就可能把当前独占的
活动模型（qwen38-27b）挤掉——冷启动大模型耗时且会中断正在生成的任务。

注意：自动切换触发点有两处，门控需同时覆盖：
- `proxy/handler.py` 的 `/v1/messages`（Anthropic 协议端点，`_handle_messages` PR-2b）；
- `proxy/chat_handlers.py` 的 `handle_chat`（`/v1/chat/completions`、`/v1/completions`、`/api/chat`）——
  `handle_chat` 中同样存在 `AUTO_SWITCH` + `ensure_service` 的自动切换路径（该路径服务 DSH 的
  openai-completions 请求：`~/.dsh/settings.yaml` 中 provider `inferfabric` 使用 `api: openai-completions`，
  baseURL 指向 `http://127.0.0.1:8999/v1`）。

### 3.3 业界最佳实践（检索结论）

- **vLLM sleep/wake（L1/L2）**：L1 权重留在显存 → 亚秒级唤醒；L2 权重卸到 CPU。vLLM 0.23.0 的
  L2 wake 有 bug（`wake_up` 报 CUDA invalid argument），故 IF 当前 `wake_vllm` 走 kill + 冷启动
  （`process_manager/vllm.py` L297-306）。L1 无此 bug。
- **版本更正（2026-09-10 实测）**：本地 conda 环境 `Qwen3.8-27B-VL` 实际安装的是 **vLLM 0.26.0**，
  0.23.0 的 L2 wake bug 可能在 0.26 已修复 → 实施 C 前应先实测 `POST /wake_up`（L2），
  若正常则默认用 L2（可释放权重显存），L1 作为备选。
- **优雅排水（graceful drain）**：停旧模型前先暂停新生成、等在途请求跑完再强停。
- **opt-in 门控**：只有显式声明要切换的调用方才触发自动切换，保护探针/默认模型请求。
- **多模型服务**：独立权重用"每模型一个进程 + 代理路由"（IF 现有模式）；同一基座多用途才用 LoRA
  适配器——**本地场景全是不同模型，LoRA 不适用**（之前方案的偏差，已更正）。

## 4. 修正后的方案（按本地实际：不同模型、单卡 32GB、无 LoRA）

| # | 措施 | 内容 |
|---|---|---|
| A | **opt-in 请求头门控** | `proxy/handler.py` `/v1/messages`：仅带 `X-IFF-AUTO-SWITCH: true` 头的请求才触发 `ensure_service`；不带头的探针/默认模型请求不触发切换（继续路由到当前活动模型或 503+Retry-After）。~20 行改动。 |
| B | **优雅排水** | `_switch_exclusive` 硬停旧模型前：先调 vLLM `POST /pause?mode=wait`（dev/rlhf 路由；`pause_generation` 只是引擎方法名，HTTP 路由是 `/pause`），等在途请求跑完（可配超时 ~30s，超时后降级 `mode=abort`）再强停，避免活动任务中断。 |
| C | **L1 睡眠/唤醒** | `sleep_vllm` 由 L2 改 L1（`POST /sleep?level=1`，权重驻留显存，仅释放 KV cache）；`wake_vllm` 改调 `POST /wake_up`（该端点只接受 `tags` 查询参数，**没有 `level` 参数**，不带参数即唤醒全部），亚秒唤醒，不再 kill 进程冷启动。 |

**VRAM 约束（单卡 32GB）**：

- L1 睡眠的模型权重仍占显存（qwen38-27b ~17GB）。32GB 单卡上，L1 睡眠模型 + 部署另一个大模型
  可能超过 97% 预算——`_deploy_model`/`_shared_add_service` 的预算检查会拦截。
- 因此 L1 适用于：**同一模型稍后唤醒**（免冷启动）与**小+大模型共存**（如 bge-m3 这类小模型 +
  一个大模型）。
- 两个大模型间的 exclusive→exclusive 交换仍是"停旧→启新"（VRAM 硬约束，非设计缺陷）。

**架构结论**：每个独立权重模型一个 vLLM 进程 + 代理按模型名路由（`/v1/models` 发现 + 请求头 model 字段
路由），这正是 IF 现有设计，对"都是不同模型"的场景是正确选择，无需引入 LoRA。

### 4.1 客户端请求头支持调查（DSH / Codex / Claude Code）

调查问题：各 agent 在标准协议层提交请求时能否附加 `X-IFF-AUTO-SWITCH` 头？结论：**三家都可配置、无需改代码**，
但闭源客户端存在版本依赖，需要代理侧提供回退模式。

| 客户端 | 附加请求头的方式 | 证据 | 备注 |
|---|---|---|---|
| DSH（本 harness） | `~/.dsh/settings.yaml` 的 provider 配置里加 `headers: { "X-IFF-AUTO-SWITCH": "true" }` | dsh-llm-pi-ai 将 provider 级 `headers` 合并进每个请求（pi-ai `mergeHeaders(auth.headers, providerOrModel.headers)`，models.js L200-260） | DSH 请求走 openai-completions（`~/.dsh/settings.yaml`: provider `inferfabric`, baseURL `http://127.0.0.1:8999/v1`）；DSH 固定发送 `User-Agent: deepseek-harness/<ver>` 归因头（dsh-llm `attributionHeaders()`） |
| Codex CLI | `~/.codex/config.toml`：`[model_providers.<id>]` 下 `http_headers = { "X-IFF-AUTO-SWITCH" = "true" }` | openai/codex config.toml 支持 `http_headers` / `env_http_headers`（config 分层：CLI > profile > 用户） | 开源，配置即生效 |
| Claude Code | 环境变量 `ANTHROPIC_CUSTOM_HEADERS`（`Name: Value` 格式，多对换行分隔） | 官方文档（code.claude.com）；需 v2.1.227+ | **旧版本不支持**；旧版 Claude Code 无法带头 → 代理需回退模式 |

**回退设计**：代理侧增加 `IFF_AUTO_SWITCH_MODE`（`header`（默认）/ `always` / `never`）。
- `header`（默认）：仅带头请求触发切换；
- `always`：保持旧行为（任意请求都触发），兼容无法加头的旧版 Claude Code 等闭源客户端；
- `never`：完全关闭请求驱动切换（仅保留 `/switch` 手动切换）。

**可选增强**：DSH 固定发送 `User-Agent: deepseek-harness/...`，代理可把已知 agent UA 也视为 opt-in
（白名单），作为 header 门控的补充手段。

## 5. 待实施改动清单

| 文件 | 改动 | 状态 |
|---|---|---|
| `inferfabric/proxy/handler.py` | A：`X-IFF-AUTO-SWITCH` 请求头门控（~20 行） | 待实施 |
| `inferfabric/proxy/chat_handlers.py` | A：`handle_chat` 同样加门控（openai-completions 路径，服务 DSH/Codex） | 待实施 |
| `inferfabric/model_lifecycle.py` | B：`_switch_exclusive` 停旧模型前优雅排水（`POST /pause?mode=wait` + 在途等待，超时降级 abort） | 待实施 |
| `inferfabric/process_manager/vllm.py` | C：`sleep_vllm` 改 L1，`wake_vllm` 改 `POST /wake_up`（无 level 参数；先实测 0.26.0 的 L2 wake 再定默认 level） | 待实施（已确认环境为 vLLM 0.26.0，0.23.0 的 L2 wake bug 需实测复验） |
| 代理配置 | `IFF_AUTO_SWITCH_MODE=header\|always\|never` 回退开关 | 待实施（兼容无法加头的闭源客户端） |

已完成（本次分析中修正）：

- `inferfabric/manager.py`：三处 V1 陈旧注释/docstring 更新为增量热插拔表述（L53 / L274 / L388）。
- `~/.config/systemd/user/inferfabric-proxy.service`：`EDGE_AUTO_SWITCH=1` 已启用并重启验证（proxy 127.0.0.1:8999 正常）。
- 环境核实：conda env `Qwen3.8-27B-VL` 中 vLLM 实际为 **0.26.0**（`python -c "import vllm; print(vllm.__version__)`），
  L2 wake bug（0.23.0）是否已修复待实测。
- 当前运行态：`gpu_mode=exclusive`，active = `qwen38-27b-abliterated`（:8002）+ `bge-m3`（:11441, gpu_role=none），
  GPU 占用 31053/32607 MiB（95.2%）——与文档背景一致。
