# G-1b: Streaming Usage Extraction — Spec

## Problem

流式请求 (`stream=True`) 转发时，`_forward_request` 盲目 `resp.read(8192)` 转发字节块，不解析 SSE 行结构。vLLM/OpenAI 的 `usage` 字段在最后一个 `data: {...}` chunk 中（`data: [DONE]` 之前），但 8192 字节块边界可能与 SSE 行边界不对齐。

**当前状态**：流式请求的 `tokens_in`/`tokens_out` 始终为 0，丢失关键计费数据。

## Solution: SSE Line Buffer

在流式转发路径中引入行缓冲，按 `\n\n` 分割 SSE 事件，解析完整 JSON 行提取 usage。

### 核心机制

1. **Line Buffer**: 累积 `resp.read()` 的字节到 buffer，按 `\n\n` 分割出完整 SSE 事件
2. **逐事件解析**: 对每个 `data: {...}` 行尝试 `json.loads`，检查是否包含 `usage` 字段
3. **最后 usage 胜**: 每次遇到 `usage` 都更新，流结束时最后一个就是最终值
4. **零延迟透传**: 解析和转发在同一循环中，不缓存完整事件再转发——收到字节立即转发，解析是旁路观察

### 设计约束

- **不增加流延迟**: 解析是旁路观察（observe-only），不阻塞转发
- **容错**: JSON 解析失败静默跳过，不影响流转发
- **向后兼容**: `stream=False` 路径不变；usage 提取失败时 tokens 保持 0
- **Ollama 路径**: Ollama native 流已有行解析，直接提取 `prompt_eval_count`/`eval_count`

### 数据流

```
vLLM response → resp.read(8192) → line_buffer → forward to client
                                      ↓
                               parse SSE lines
                                      ↓
                               extract usage (if present)
                                      ↓
                               set handler._usage (prompt_tokens, completion_tokens)
```

### 接口变更

```python
# handler 新属性
handler._usage = {"prompt_tokens": 0, "completion_tokens": 0}

# _forward_request 流式路径新增参数
def _forward_request(handler, pm, target_port, body, stream):
    # stream=True 时:
    # 1. 透传字节（不变）
    # 2. 行缓冲解析 → 提取 usage → handler._usage
    pass

# handle_chat 日志记录
pm.logger.log(RequestLog(
    ...,
    tokens_in=handler._usage.get("prompt_tokens", 0) or ...,
    tokens_out=handler._usage.get("completion_tokens", 0) or ...,
))
```

### 边界条件

| 场景 | 处理 |
|------|------|
| vLLM 不返回 `usage`（`stream_options.include_usage=false`）| tokens 保持 0 |
| 最后 usage chunk 跨 8192 边界 | 行缓冲自动累积，下次 read 拼合 |
| `data: [DONE]` | 不解析，标记流结束 |
| 非 SSE 响应（content-type≠text/event-stream）| 不做行解析，保持原行为 |
| 客户端断开（BrokenPipe）| usage 已提取（之前的 chunk），正常记录 |
| 空行/注释行（`: comment`）| 跳过 |

### SSE 行缓冲实现

```python
buffer = b""
while True:
    chunk = resp.read(8192)
    if not chunk:
        break
    # 立即转发（零延迟）
    size = f"{len(chunk):x}\r\n".encode()
    handler._safe_write(size)
    handler._safe_write(chunk)
    handler._safe_write(b"\r\n")
    
    # 旁路观察：行缓冲解析
    buffer += chunk
    while b"\n\n" in buffer:
        event_bytes, buffer = buffer.split(b"\n\n", 1)
        _extract_usage_from_sse_event(handler, event_bytes)

# 流结束，处理 buffer 残余
if buffer.strip():
    _extract_usage_from_sse_event(handler, buffer)
```

### Ollama Native 流路径

已有行解析，直接从最后一个 JSON 对象提取：

```python
# 在 handle_ollama_native 的 stream 分支中：
# 最后一个 chunk 可能包含 eval_count/prompt_eval_count
# 在流结束时从 resp 读取统计（Ollama 流不返回 usage，需估算或跳过）
```

**注意**: Ollama 流式响应每行一个 JSON，但 `eval_count`/`prompt_eval_count` 仅在非流式响应中返回。流式路径需在 `data: [DONE]` 发送后补充日志更新。

### 不做的事

- **不修改上游请求**：不强制添加 `stream_options: {include_usage: true}`（由客户端控制）
- **不缓存完整响应**：只提取 usage，不重组完整 content
- **不做 token 估算**：没有 usage 就记 0，不猜

## 影响范围

| 文件 | 变更 |
|------|------|
| `chat_handlers.py` | `_forward_request` 流式路径加行缓冲 + usage 提取 |
| `chat_handlers.py` | `handle_chat` 日志记录使用 `handler._usage` |
| `chat_handlers.py` | `handle_ollama_native` 流式路径 usage 提取 |

## 测试

1. 模拟 SSE 流：构造含 usage 的 chunk 序列，验证提取正确
2. chunk 跨边界：usage chunk 被拆成两个 `resp.read` 返回
3. 无 usage 场景：`stream_options.include_usage=false`，tokens 保持 0
4. 非 SSE 响应：content-type 为 `application/json`，不触发行解析
5. Ollama 流式：验证不崩溃（无 usage 时 tokens=0）
6. 并发流式请求：多请求同时流，usage 不串
