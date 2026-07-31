# Feature Spec: v4.6 API Gateway

**Version**: delta-001
**Date**: 2026-07-30
**Status**: Draft

## 概述

IFF v4.6 将代理层升级为完整的 API Gateway，新增鉴权、日志、限流器重构、云端 Provider 统一管理四大模块。

## 核心定位

IFF 是**单用户 LLM Hub** — API Gateway + 模型部署管理 + 资源管理。不需要多租户能力。

## PR 清单

### PR-A: 鉴权

**需求**: 单 key 锁门 + 可选临时 guest key

- `api_keys.yaml` 配置：primary key（长期有效，全模型）+ 可选 guest keys（模型白名单 + 过期时间）
- 文件不存在或为空 = 不开启鉴权（当前行为不变）
- Bearer token 校验在 `do_POST` 入口，admin 路由不受影响
- 热加载：`iff reload` 重新读取 yaml

**验收标准**:
- Given 未配置 api_keys.yaml, When 发送请求, Then 行为与 v4.3 完全一致
- Given 配置了 primary key, When 发送无 Authorization 请求, Then 返回 401
- Given 配置了 primary key, When 发送正确 Bearer token, Then 正常转发
- Given guest key 的 models=["qwen35-9b"], When 请求 qwen36-35b, Then 返回 401 + "model not allowed"
- Given guest key 的 expires 已过, When 发送请求, Then 返回 401 + "key expired"

### PR-B: 结构化请求日志

**需求**: 每个请求完成时写一行 JSONL，按日轮转

- `RequestLogger` 类，输出 `logs/access-YYYY-MM-DD.jsonl`
- 每行字段：req_id, key_name, model, status, ttft_ms, tokens_in, tokens_out, duration_ms, route(local/cloud), cloud_provider, error, ts
- TTFT 从 SSE 流首 token 时间戳解析
- `iff.yaml` 新增 `access_log: true`（默认 true）
- 实时 flush，防 crash 丢数据

**验收标准**:
- Given access_log=true, When 发送 3 个请求, Then access.log 有 3 行 JSONL
- Given access_log=false, When 发送请求, Then 无日志文件生成
- Given 跨日请求, When 第二天到来, Then 自动轮转到新文件
- Given SSE 流请求, When 首 token 到达, Then ttft_ms 字段有值

### PR-C: 限流器重构

**需求**: `threading.Semaphore` → `asyncio` 原生 + 两级门控

- `AsyncTokenBucket` 令牌桶，asyncio.Lock + asyncio.wait_for
- 两级：server 级总并发 + per-model 并发
- 启动时预创建所有 model bucket（消除首次竞态）
- 删除 `_MODEL_RATE_LIMITERS` dict 和 `clear()` 竞态
- `forwarder.py` 的 aiohttp ClientSession 加 TCPConnector keepalive

**验收标准**:
- Given c=4 并发请求, When 各请求 TTFT 标准差 < 均值 20%, Then 限流器不阻塞事件循环
- Given 未注册的 model_name, When 请求到达, Then 返回 404 而非创建新 limiter
- Given server 级并发已满, When 新请求到达, Then 返回 429

### PR-D: 云端 Provider 统一管理

**需求**: `cloud_provider.yaml` 双协议 + 自动发现

- 配置文件：per-provider 的 api_key + openai_base + anthropic_base
- 双协议原生透传：客户端走 OpenAI → 转发到云端 OpenAI 端点；Anthropic → Anthropic 端点；IFF 不做协议转换
- 自动发现：启动时 GET `<openai_base>/models`，应用 filter 规则，合入路由表
- 定期轮询发现新模型/移除下线模型（interval 可配）
- IFF 持有凭证，客户端只需 IFF key
- 删除 `model_affinity.yaml`，路由逻辑统一由 `ProxyManager._cloud_models` 驱动
- /v1/models 合并返回本地 + 云端模型

**验收标准**:
- Given 配置了 baidu-codingplan, When IFF 启动, Then 自动发现云端模型并合入路由表
- Given 客户端请求 /v1/chat/completions + model=deepseek-v4-flash, When 路由到云端, Then 转发到 openai_base/chat/completions
- Given 客户端请求 /v1/messages + model=deepseek-v4-flash, When 路由到云端, Then 转发到 anthropic_base/messages
- Given 云端模型请求, When IFF key 验证通过, Then 使用 IFF 持有的凭证转发（非客户端透传）
- Given 本地模型失败, When local_fallback=true, Then 自动回退到云端同名模型
- Given provider 只有 openai_base, When 客户端走 Anthropic 协议, Then 返回 501
- Given /v1/models 请求, When 本地+云端都有模型, Then 合并返回所有模型

## 依赖关系

```
PR-A (鉴权) ──┐
               ├──→ PR-D (云端路由, 依赖 A 凭证 + B 日志)
PR-B (日志) ───┘
PR-C (限流) ─── 独立，可与 A/B 并行
```

## 风险声明

| 风险 | 爆炸链 | 缓解 |
|------|--------|------|
| PR-C handler async 桥接 | handler 是同步 BaseHTTPRequestHandler → asyncio 调用失败 → 全链路挂 | 用 asyncio.run_coroutine_threadsafe 桥接，不重写 handler |
| PR-D 路由逻辑变更 | 路由表错误 → 请求发到错误端点 → 数据泄露/超时 | 双写期（新旧路由并存），冒烟验证后再删除旧代码 |
| PR-D 云端凭证安全 | api_key 写入 yaml → 文件权限泄露 | yaml 设 600 权限，gitignore 排除 |

## 冻结合约

- 所有修改仅在 `~/projects/inferfabric-sandbox/` 沙箱完成
- 不合入 `~/projects/inferfabric/` 生产环境
- 完成后提交修改总结 + 测试报告供 Vincent 审查
