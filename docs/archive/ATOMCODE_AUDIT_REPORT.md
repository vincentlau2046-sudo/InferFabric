# InferFabric 代码审计报告 — AtomCode v4.25.9 (DeepSeek V4 Flash)

**审计日期**: 2026-07-04
**审计范围**: `inferfabric/` 全部模块（~4,200 行 Python）、测试套件、shell 脚本、YAML 配置
**代码版本**: v4.3.0 (README) / v4.0.0 (`__init__.py`)

---

## 1. 架构设计合理性

| 维度 | 评价 |
|------|------|
| 模块化 | ✅ "模型即插件"，增删零代码改动 |
| 状态机 | ✅ 三态 GPU（idle/exclusive/shared），`validate_transition()` 完整 |
| 分层 | ✅ config→state→gpu_lock→health→process_manager→manager→proxy/cli |
| 进程隔离 | ✅ start_new_session + PGID + fuser 端口级兜底 |
| 降级策略 | ✅ Baidu 云降级 + 指数退避重试 |
| 故障恢复 | ✅ reconcile + force_reset + iff-recovery.sh 三级恢复 |
| 缺陷 | ⚠️ forwarder.py 与 proxy.py 存在 send_json/read_body 重复代码 |

---

## 2. CRITICAL（4项）

### C1. Proxy 完全无认证
- **文件**: `proxy.py:30, L191-259`
- **问题**: 所有端点对 LAN 开放；若绑定 0.0.0.0 则整个 LAN 可执行模型切换或调用 LLM
- **建议**: 加 API Key 认证

### C2. `nvidia-smi --gpu-reset` 破坏性重置
- **文件**: `manager.py:886-890`
- **问题**: 对所有 GPU 进程执行 SIGKILL + 重置 GPU 状态，影响 X server 及其他 CUDA 任务
- **建议**: 移除或加显式确认 + GPU_FREE_THRESHOLD 判断

### C3. `pkill -9 -f "python main.py"` 误杀
- **文件**: `process_manager.py:478, 625`
- **问题**: 匹配任意含 `python main.py` 的进程，误杀无关服务
- **建议**: 改用精确路径匹配 `pkill -f "ComfyUI.*main.py"`

### C4. 流式连接泄漏
- **文件**: `proxy.py:404-421`
- **问题**: 客户端断开后上游连接未主动关闭，保持直到 300s 超时
- **建议**: try/finally 确保 conn.close()

---

## 3. HIGH（6项）

| ID | 文件 | 问题 |
|----|------|------|
| H1 | proxy.py:173 | 类级共享 dict 无锁保护（ThreadedHTTPServer） |
| H2 | manager.py:588 | VRAM 检查竞态，并发 switch 可能双启动 |
| H3 | state.py:234 | set_sleep_state() TOCTOU 竞态 |
| H4 | manager.py:377 | Config drift 检测依赖 /proc cmdline，脆弱 |
| H5 | forwarder.py:186 | retry 分支未关闭 conn |
| H6 | dashboard.py:571 | Dashboard JS 锁可被双 tab 绕过 |

---

## 4. MEDIUM（8项）

M1: `/v1/models` 串行 HTTP 调用
M2: `yaml.safe_load` 不 catch 异常
M3: `_pkill_vllm_fallback` 硬编码端口
M4: `set_multi` 非原子写入
M5: `send_json`/`read_body` 重复代码
M6: `wait_http` 无限重试风险
M7: `discover_local_models` 无深度限制
M8: `add_history` 并发 DELETE 竞争

---

## 5. LOW（7项）

L1: 版本字符串漂移 (4.0.0 vs 4.3.0)
L2: 大量惰性导入
L3: _VALID_TRANSITIONS 缺注释
L4: proxy.log 二进制提交到 git
L5: 无 HTTP 连接池
L6: 类型签名冗余
L7: GPU baseline JSON 并发写

---

## 6. 可维护性评估

| 维度 | 评分 |
|------|------|
| 模块内聚 | A |
| 注释文档 | B- |
| 测试覆盖 | B+ |
| 向后兼容 | A |
| 依赖管理 | A- |
| 代码重复 | B |
| 错误处理 | B |
| 线程安全 | C+ |

---

## 7. 关键改进建议

### P0 — 必须修复
1. Proxy 添加 API Key 认证
2. 移除 `nvidia-smi --gpu-reset`
3. `_vllm_gen_counters` 加线程锁
4. `pkill` 改用精确路径匹配

### P1 — 建议修复
5. StateDB TOCTOU 修复
6. Config drift 改为 YAML 内容哈希
7. retry 分支补 conn.close()
8. 流式断开时关闭上游连接

### P2 — 可改善
9. reconcile() 并行化
10. load_models() YAML 容错
11. 版本号统一
12. proxy.log 加 .gitignore
