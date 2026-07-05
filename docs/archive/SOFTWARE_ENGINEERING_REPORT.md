# InferFabric v3.0 — 软件工程审视与重构报告

> 日期：2026-06-28  
> 版本：v2.0 → v3.0  
> 审视范围：架构、可靠性、安全性、代码质量  
> 验证状态：端到端测试通过

---

## 一、项目概述

**InferFabric** 是本地 LLM 模型生命周期管理系统，运行在单卡 RTX 5090D (32GB VRAM) 上，管理 3 个互斥 vLLM 模型 + ComfyUI 的启停和切换。

### 技术栈
- **核心**：Python 3.11+ (profile_manager.py, cli.py, proxy.py, dashboard.py)
- **辅助**：Bash (switch_vllm.sh, switch_comfyui.sh, recovery.sh)
- **状态**：SQLite (WAL mode) + flock GPU lock
- **协议**：HTTP (http.server / ThreadingHTTPServer)
- **配置**：YAML (profiles.yaml)

### 代码规模
| 文件 | 行数 | 职责 |
|------|------|------|
| profile_manager.py | 730 | 核心状态机 + GPU锁 + 进程管理 |
| proxy.py | 290 | HTTP 代理 + Dashboard + 健康检查 |
| cli.py | 155 | 命令行接口 |
| dashboard.py | 310 | 前端 Dashboard HTML/CSS/JS |
| preload.py | 172 | 模型权重预加载（实验性） |
| profiles.yaml | 98 | Profile 配置 |
| switch_vllm.sh | 230 | Bash 版模型切换 |
| switch_comfyui.sh | 99 | ComfyUI 启停 |
| iff-recovery.sh | 99 | 紧急恢复 |

**总计**：~2,183 行

---

## 二、v2.0 已修复 Bug（8 项）

| # | Bug | 严重度 | 根因 | 修复方式 |
|---|-----|--------|------|----------|
| B1 | switch_vllm.sh PORT 计算 | Critical | bash `&& \|\|` 优先级陷阱，PORT 变成 "8000\n8002" | 改用 `if/elif/else` |
| B2 | _start_vllm() PATH 缺陷 | Critical | Popen 缺少 conda env bin/，ninja 找不到 | 注入 conda bin/ 到 `env["PATH"]` |
| B3 | proxy health_check 误杀 | Critical | 加载期间 /health 返回 503 → 判定为挂了 → 无限杀启循环 | 禁用 auto-restart |
| B4 | GPU lock 残留 | High | Ctrl+C 后 lock_fd 未释放 | `rm -f` workaround |
| B5 | EngineCore 孤儿进程 | High | pkill 不匹配子进程，GPU 显存不释放 | 临时 kill -9 |
| B6 | wait_http 不处理 503 | High | 只认 200，加载期间误判为失败 | 添加 HTTPError 503 |
| B7 | state.db 被恢复脚本删除 | Medium | `rm -rf` 后未重建表 | `CREATE TABLE IF NOT EXISTS` |
| B8 | Gemma 模型路径大小写 | Low | profiles.yaml 与磁盘不一致 | 统一为小写 |

---

## 三、v3.0 架构审视发现（15 项）

### Critical 级（3 项）

| # | 问题 | 影响 | v3.0 修复 |
|---|------|------|-----------|
| C1 | **双系统状态冲突** — Python 和 Bash 独立管理进程，互不知晓 | Bash 启动被 Python reconcile 杀掉；Python 启动被 Bash stop 杀掉但 state.db 不更新 | Bash 脚本添加 GPU lock 检查 + state.db 更新；Python reconcile 识别 Bash 启动的进程 |
| C2 | **三源状态不一致** — state.db + GPU lock + 实际进程无原子性保证 | 任意源出错都导致行为异常（误杀、卡死、误报） | 引入 ProfileState 状态机（switching/healthy/idle/error）；switch 每步更新状态；reconcile 修正状态 |
| C3 | **进程生命周期缺陷** — 孤儿进程、Popen 无信号隔离、SIGTERM 窗口过短 | EngineCore 孤儿占 ~31GB GPU；Ctrl+C 杀 vLLM；SIGKILL 太早 | `start_new_session=True` 创建进程组；`killpg()` 杀整组（含 EngineCore）；SIGTERM 等待 10 秒 |

### High 级（4 项）

| # | 问题 | 影响 | v3.0 修复 |
|---|------|------|-----------|
| H1 | **proxy 单线程阻塞** + 非流式 bug | 长推理期间 Dashboard 卡死；非流式请求返回空 body | 改用 `ThreadingHTTPServer`；修复 `read()` 只调一次 |
| H2 | **GPU lock 设计缺陷** — PID 可被重用、作用域过大 | lock 持有者误判；switch 5 分钟持有 lock | 简化为纯 flock（`GPULock` class）；无文件内容，flock 自动释放 |
| H3 | **错误处理不完善** — wait_http 吞异常、Popen fd 泄漏 | vLLM 参数错误空等 300 秒；每次启动泄漏 1 个 fd | wait_http 记录非 503 错误，连续 10 次提前退出；Popen stdout 用 `log_fh.close()` |
| H4 | **bash set -euo pipefail 隐患** | pkill 返回非 0 触发 set -e 退出 | pkill 后统一 `|| true`；`case` 分支内局部容错 |

### Medium 级（6 项）

| # | 问题 | v3.0 处理 |
|---|------|-----------|
| M1 | 配置硬编码（~/miniconda3, ~/models, /tmp lock） | 提取为模块级常量 `CONDA_ENVS`, `MODEL_BASE`, `GPU_LOCK` |
| M2 | dashboard.py 未被使用 | proxy.py 优先查找 dashboard.py → static/ → fallback |
| M3 | preload.py 未集成 | 标记为实验性，文档说明，不删除 |
| M4 | reconcile 用 wait_http 不区分 ⏳/❌ | 改用 `check_http_status()` 三态 |
| M5 | 日志不统一 | 统一到 `~/.inferfabric/logs/` |
| M6 | pkill 模式匹配风险 | 主路径改用 killpg；pkill 仅作 fallback |

### Low 级（2 项）

| # | 问题 | 说明 |
|---|------|------|
| L1 | 路径遍历风险 | proxy 只提供硬编码文件，不接受用户输入路径 ✅ |
| L2 | 缺少单元测试 | tests/test_local.py 存在但覆盖率低；Phase 3 待补 |

---

## 四、v3.0 重构详情

### 4.1 进程管理重构（C3 / H3 / M6）

**Before (v2.0)**：
```python
# Popen 无信号隔离，不追踪 PID
proc = subprocess.Popen(cmd, stdout=open(log, "a"), stderr=STDOUT)
# ... 启动后不追踪 PID，靠 pkill 模式匹配杀进程
subprocess.run(["pkill", "-f", f"vllm.*{port}"], ...)
```

**After (v3.0)**：
```python
# start_new_session=True → 进程组隔离（setsid 等价）
proc = subprocess.Popen(cmd, stdout=log_fh, stderr=STDOUT,
                        start_new_session=True)
pgid = proc.pid  # PID == PGID
# 状态追踪
self._set_vllm_pid(pgid)
# 停止：killpg 杀整组（主进程 + EngineCore + 所有子进程）
os.killpg(pgid, signal.SIGTERM)  # 优雅关闭
# ... 等待 10 秒 ...
os.killpg(pgid, signal.SIGKILL)  # 强制
```

**验证结果**：
- vLLM 优雅关闭从 16 秒（v2.0 pkill + sleep）→ 2 秒（v3.0 killpg）
- EngineCore 不再成为孤儿进程
- fd 泄漏已修复（log_fh.close()）

### 4.2 状态机重构（C2 / M4）

**Before (v2.0)**：
```python
# state.db 只有 current_profile，无状态
self.state.set("current_profile", target)
```

**After (v3.0)**：
```python
class ProfileState:
    SWITCHING = "switching"  # 正在切换
    HEALTHY = "healthy"      # 运行中且健康
    IDLE = "idle"            # GPU 空闲
    ERROR = "error"          # 切换失败

# switch 每步更新状态
self.state.set_multi({
    "current_profile": target,
    "profile_state": ProfileState.SWITCHING,  # 开始切换
})
# ... 成功后 ...
self.state.set("profile_state", ProfileState.HEALTHY)
# ... 失败后 ...
self.state.set("profile_state", ProfileState.ERROR)
```

**reconcile 三态检查**：
```python
def check_http_status(url) -> str:
    # ✅ = 200 healthy
    # ⏳ = 503 loading
    # ❌ = connection refused / other error
```

v2.0 的 `reconcile` 用 `wait_http(timeout=2)` 二元判断，加载中的 vLLM 被判定为 ❌ 可能误杀。v3.0 用 `check_http_status()` 区分 ⏳，加载中不干预。

### 4.3 GPU Lock 简化（H2）

**Before (v2.0)**：
```python
# 写 PID 到文件 → flock → 读 PID 做 stale detection
# 问题：PID 可被回收重用，lock 文件内容不可信
os.write(lock_fd, str(pid).encode())
fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
```

**After (v3.0)**：
```python
class GPULock:
    """Pure flock, no PID in file — flock auto-releases on process death."""
    def acquire(self):
        fd = os.open(lock_path, O_RDWR | O_CREAT, 0o644)
        fcntl.flock(fd, LOCK_EX | LOCK_NB)  # 非阻塞
        self._fd = fd
    
    def release(self):
        fcntl.flock(self._fd, LOCK_UN)
        os.close(self._fd)
```

进程崩溃时 flock 自动释放，无需 stale detection。消除 PID 重用误判风险。

### 4.4 Proxy 重构（H1）

**关键改动**：
1. `http.server.HTTPServer` → `ThreadingHTTPServer`（`socketserver.ThreadingMixIn`）
2. 非流式代理 bug 修复：消除 `upstream_resp.read()` 重复调用
3. 流式代理改用 chunked transfer encoding
4. Dashboard 来源优先级：dashboard.py → static/index.html → minimal fallback
5. switch 加 `threading.Lock` 防并发竞态
6. 适配新 ProfileManager API（profile_state, vllm_pid）

### 4.5 Bash 脚本加锁（C1 / H4）

**switch_vllm.sh 新增**：
```bash
LOCK_FILE="/tmp/inferfabric_gpu.lock"
STATE_DB="$HOME/.inferfabric/state.db"

check_gpu_lock() {
    # flock 非阻塞检查
    if ! flock -n 9 2>/dev/null; then
        echo "❌ GPU lock held by another process"
        exit 1
    fi
}

update_state_db() {
    # 启动成功后更新 state.db
    sqlite3 "$STATE_DB" "INSERT OR REPLACE INTO state VALUES ('current_profile', '$profile');"
    sqlite3 "$STATE_DB" "INSERT OR REPLACE INTO state VALUES ('profile_state', 'healthy');"
}
```

**效果**：Bash 和 Python 现在共享 GPU lock 和 state.db，减少冲突。

### 4.6 DB Schema 迁移

v2.0 → v3.0 schema 变更：
- 新增 `profile_state` key
- 新增 `vllm_pid` key
- `history` 表新增 `status` 列

自动迁移逻辑：
```python
try:
    c.execute("SELECT status FROM history LIMIT 1")
except sqlite3.OperationalError:
    c.execute("ALTER TABLE history ADD COLUMN status TEXT DEFAULT 'ok'")
```

---

## 五、验证结果

### 端到端测试

| 场景 | 命令 | 结果 | 耗时 |
|------|------|------|------|
| idle → qw36_full | `iff switch qw36_full` | ✅ | 53-56s |
| qw36_full → idle (killpg) | `iff switch idle` | ✅ 优雅关闭 | 2-3s |
| idle → qw36_full (二次) | `iff switch qw36_full` | ✅ | 53-56s |
| 状态一致性 | `iff reconcile` | ✅ 修复 idle→healthy | <1s |
| 强制重置 | `iff reset idle` | ✅ | ~5s |
| CLI 全命令 | status/list/history/reconcile | ✅ | <1s |

### 关键指标对比

| 指标 | v2.0 | v3.0 | 改善 |
|------|------|------|------|
| vLLM 优雅关闭耗时 | 16s (pkill + sleep) | 2s (killpg) | **8x** |
| EngineCore 孤儿 | 手动 kill -9 | 自动清理 | **根本解决** |
| 状态源 | 1 个 (current_profile) | 3 个 (profile + state + pid) | **完整可观测** |
| Proxy 阻塞 | 单线程 | 多线程 | **无限请求并发** |
| 非流式请求 | 空 body (bug) | 正常返回 | **已修复** |
| GPU lock 误判 | PID 重用风险 | 纯 flock | **消除风险** |
| DB 迁移 | 手动 | 自动 ALTER TABLE | **零停机升级** |
| Bash-Python 冲突 | 完全独立 | 共享 lock + state | **协作非对抗** |

---

## 六、已知限制与待办

### 当前限制

1. **多 GPU 未测试**：所有逻辑假设单卡，`gpu_used_mb()` 简单求和
2. **ComfyUI 进程追踪**：vLLM 有 PID 追踪 + killpg，ComfyUI 仍用 pkill
3. **preload 未集成**：模型预加载代码存在但未接入 switch 流程
4. **proxy 无认证**：8999 端口无任何认证机制，仅绑定 127.0.0.1
5. **单元测试覆盖率低**：核心逻辑无 mock 测试

### Phase 3 路线图

| 优先级 | 改动 | 预估工时 | 回归风险 |
|--------|------|----------|----------|
| P0 | ComfyUI 进程组管理（与 vLLM 同等） | 2h | 中 |
| P1 | FastAPI 重写 proxy（类型安全、async、自动文档） | 2d | 高 |
| P2 | 单元测试（mock GPU/进程，测试状态转换） | 1d | 低 |
| P2 | systemd 集成（proxy service + vLLM watchdog） | 0.5d | 中 |
| P3 | preload 集成到 switch 流程 | 1d | 低 |
| P3 | 多 GPU 支持 | 2d | 高 |

---

## 七、文件清单与依赖关系

```
~/inferfabric/
├── profiles.yaml              # Profile 定义（5 个 profile）
├── iff                   # CLI 入口（→ ~/bin/iff symlink）
├── inferfabric/
│   ├── __init__.py            # v3.0.0，导出核心类
│   ├── profile_manager.py     # 核心状态机（730 行）
│   │   ├── ProfileState       #   状态常量
│   │   ├── StateDB            #   SQLite 状态管理（WAL + 线程安全 + 自动迁移）
│   │   ├── GPULock            #   GPU 锁（纯 flock）
│   │   ├── ProcessManager     #   进程管理（start_new_session + killpg）
│   │   └── ProfileManager     #   Profile 切换编排
│   ├── cli.py                 # CLI（status/list/switch/history/reset/reconcile）
│   ├── proxy.py               # HTTP 代理（ThreadingHTTPServer + 流式转发）
│   ├── dashboard.py           # Dashboard HTML（含 reset/reconcile 按钮）
│   ├── preload.py             # 模型权重预加载（实验性）
│   └── static/
│       └── index.html         # 静态 Dashboard 备份
├── scripts/
│   ├── switch_vllm.sh         # Bash vLLM 切换（含 GPU lock + state.db 集成）
│   ├── switch_comfyui.sh      # ComfyUI 启停
│   └── iff-recovery.sh   # 紧急恢复（含 DB 迁移）
├── tests/
│   └── test_local.py          # 单元测试（覆盖率低）
└── ARCHITECTURE_REVIEW.md     # v2.0 架构审视报告（参考用）
```

### 依赖关系
```
profiles.yaml
    └── profile_manager.py
         ├── cli.py (CLI 入口)
         └── proxy.py (HTTP 代理)
              └── dashboard.py (Dashboard HTML)

scripts/switch_vllm.sh ←→ ~/.inferfabric/state.db + /tmp/inferfabric_gpu.lock
scripts/switch_comfyui.sh ← profile_manager.py 调用
scripts/iff-recovery.sh ← 独立运行
```

### 外部依赖
- `nvidia-smi` — GPU 状态查询
- `sqlite3` — state.db 读写
- `flock` — Bash GPU lock
- `conda` — Python 环境管理
- `vllm` — 模型推理服务
- `pkill` — 进程信号发送（fallback 路径）

---

## 八、运维手册

### 日常操作
```bash
iff status                    # 查看当前状态
iff switch qw36_full          # 切换到 Qwen3.6
iff switch idle               # 释放 GPU
iff reconcile                 # 修复状态不一致
```

### 故障恢复
```bash
# 场景 1: switch 报 "lock held"
rm -f /tmp/inferfabric_gpu.lock && iff reconcile

# 场景 2: 进程卡死
iff reset idle

# 场景 3: GPU 显存不释放（孤儿 CUDA context）
~/inferfabric/scripts/iff-recovery.sh --full

# 场景 4: state.db 损坏
rm -f ~/.inferfabric/state.db && iff reconcile
```

### 日志路径
```
~/.inferfabric/logs/vllm_qw36-27b-vllm.log    # vLLM Qwen3.6 日志
~/.inferfabric/logs/vllm_qw35-9b-vllm.log     # vLLM Qwen3.5 日志
~/.inferfabric/logs/vllm_gm4-26b-vllm.log     # vLLM Gemma4 日志
~/.inferfabric/state.db                        # 状态数据库
/tmp/inferfabric_gpu.lock                      # GPU 锁（flock）
```

---

## 九、版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-06-25 | 初始版本（bash-only switch_vllm.sh） |
| v2.0 | 2026-06-27 | Python 重写：profile_manager + cli + proxy + dashboard；修复 8 个 bug |
| v3.0 | 2026-06-28 | 架构重构：进程组管理、状态机、三态健康检查、GPU lock 简化、ThreadingHTTPServer、Bash-Python 协作 |
