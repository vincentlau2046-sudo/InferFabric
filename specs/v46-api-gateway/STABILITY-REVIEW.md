# IFF v4.6.0 生产稳定性审计 — 双 Agent 交叉汇总

**Date**: 2026-07-30
**Reviewers**: AtomCode (AtomGit-deepseek-v4-flash), OpenCode (baidu-codingplan/deepseek-v4-flash)
**Focus**: 生产系统稳定性，不是代码质量

## 维度评分对比

| 维度 | AtomCode | OpenCode | 共识 |
|------|----------|----------|------|
| D1 崩溃安全 | ⚠️ 关注 | ✅ PASS | ✅ 无进程崩溃风险，顶级 `except Exception` 覆盖全 |
| D2 资源生命周期 | ⚠️ 关注 | ❌ FAIL | ❌ 3 处 HTTP 连接泄漏（非流式路径未 close） |
| D3 线程安全 | ⚠️ 关注 | ⚠️ 关注 | ⚠️ Admin API 绕过 RLock 直接突变 dict |
| D4 故障隔离 | ✅ PASS | ⚠️ 关注 | ⚠️ 发现失败时全量替换（清空旧模型） |
| D5 热路径 | ✅ PASS | ✅ PASS | ✅ 本地 vLLM 路径零影响 |
| D6 启动 | ✅ PASS | ✅ PASS | ✅ 缺配置→本地模式，不会崩 |
| D7 错误传播 | ⚠️ 关注 | ⚠️ 关注 | ⚠️ 云端 429/404 被错误映射为 502 |
| D8 运维 | ⚠️ 关注 | ⚠️ 关注 | ⚠️ reload 后轮询丢失、日志无清理 |

## 两方共识的 MUST-FIX（不修可能出事故）

| # | 严重性 | 文件 | 问题 | 修复 | 工作量 |
|---|--------|------|------|------|--------|
| **F1** | 🔴 严重 | `forwarder.py:179` | 非流式 `forward_to_cloud` 不 close resp → FD 泄漏 | 加 `resp.close()` | S |
| **F2** | 🔴 严重 | `forwarder.py:185` | HTTPError 路径不 close e → FD 泄漏 | 加 `e.close()` | S |
| **F3** | 🔴 严重 | `forwarder.py:207` | `forward_to_baidu` 不 close resp → FD 泄漏 | 加 `resp.close()` | S |
| **F4** | 🟡 高 | `handler.py:669` | `/admin/cloud/reload` 后轮询线程永久丢失 | reload 后调用 `start_polling()` | S |
| **F5** | 🟡 高 | `handler.py:769-800` | Admin API 无锁突变 `_cloud_models` + `_providers` | 加 `_models_lock` 保护 | M |
| **F6** | 🟡 高 | `cloud_discovery.py:160` | 发现失败时全量替换→网络瞬断清空所有云模型 | 仅在有新结果时替换，否则保留旧数据 | M |
| **F7** | 🟠 中 | `forwarder.py:185` | 云端 429/404 映射为 502→客户端重试逻辑错误 | 透传上游状态码 | M |

## 建议 FIX（非阻塞但推荐）

| # | 严重性 | 问题 | 修复 |
|---|--------|------|------|
| S1 | 低 | 轮询线程无看门狗 | 周期性存活检查 + 重启 |
| S2 | 低 | JSONL 日志无清理 | 加 `max_age_days=30` |
| S3 | 低 | `_model_buckets` 无限增长 | 加 LRU 上限 |
| S4 | 低 | `providers` 属性返回可变引用 | 返回 `dict(...)` 快照 |

## 好消息（Vincent 最关心的）

1. **热路径零回归** — 本地 vLLM 代理路径完全不受影响，不走 cloud 分支
2. **不会进程崩溃** — 所有 HTTP handler 有顶级 `except Exception`，单个请求错误不会杀服务器
3. **启动健壮** — 缺配置/malformed YAML/缺环境变量 → 全部降级到本地模式，不崩
4. **认证安全降级** — 缺 `api_keys.yaml` → auth disabled，不阻止请求
5. **故障隔离** — 单个 cloud provider 挂了不影响本地/其他 provider
6. **`/switch` 不受影响** — 同步阻塞行为不变，v4.6.0 没加新路径

## 结论

**核心焦虑解答**：v4.6.0 不会让系统崩溃。热路径零回归。但如果不修 F1-F6，长时间运行（数天/数周）可能出现：
- FD 泄漏 → 新连接失败（F1-F3）
- Admin 操作后功能丢失（F4-F5）
- 网络瞬断后云模型全部消失（F6）

**修完 F1-F7 后 → 可安全合入生产**。总计改动量约 30-40 行。
