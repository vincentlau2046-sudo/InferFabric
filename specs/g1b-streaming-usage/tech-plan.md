# G-1b: Streaming Usage Extraction — Tech Plan

## Phase 1: SSE Line Buffer Utility (新增模块)

### 1.1 `inferfabric/proxy/sse_buffer.py`

```python
class SSELineBuffer:
    """SSE 流式行缓冲 + usage 提取器。
    
    纯观察者：不修改转发路径，只在旁路提取 usage。
    线程不安全（每个请求独占一个实例）。
    """
    
    def __init__(self):
        self._buffer = b""
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
    
    def feed(self, chunk: bytes):
        """喂入原始 chunk（与转发同步调用）。
        
        在 resp.read() → _safe_write() 之间调用。
        解析完整 SSE 事件，提取 usage（如果存在）。
        """
        self._buffer += chunk
        while b"\n\n" in self._buffer:
            event_bytes, self._buffer = self._buffer.split(b"\n\n", 1)
            self._parse_event(event_bytes)
    
    def flush(self):
        """流结束，处理 buffer 残余。"""
        if self._buffer.strip():
            self._parse_event(self._buffer)
            self._buffer = b""
    
    def _parse_event(self, event_bytes: bytes):
        """解析单个 SSE 事件，提取 usage。"""
        for line in event_bytes.split(b"\n"):
            line = line.strip()
            if not line or line.startswith(b":"):
                continue
            if line.startswith(b"data: "):
                data_str = line[6:]
            elif line.startswith(b"data:"):
                data_str = line[5:]
            else:
                continue
            if data_str.strip() == b"[DONE]":
                return
            try:
                obj = json.loads(data_str)
                usage = obj.get("usage")
                if usage and isinstance(usage, dict):
                    pt = usage.get("prompt_tokens", 0) or 0
                    ct = usage.get("completion_tokens", 0) or 0
                    if pt > 0 or ct > 0:
                        self.usage["prompt_tokens"] = pt
                        self.usage["completion_tokens"] = ct
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
```

## Phase 2: 集成到 `_forward_request`

### 2.1 流式路径变更

```python
def _forward_request(handler, pm, target_port, body, stream):
    # ... existing code ...
    
    if stream:
        headers_sent = True
        # ... existing headers ...
        
        ttft_recorded = False
        sse_buf = SSELineBuffer()  # NEW
        
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                if not ttft_recorded:
                    ttft_recorded = True
                    if hasattr(handler, '_req_start'):
                        handler._ttft_ms = (time.monotonic() - handler._req_start) * 1000
                
                # 立即转发（零延迟，不变）
                size = f"{len(chunk):x}\r\n".encode()
                handler._safe_write(size)
                handler._safe_write(chunk)
                handler._safe_write(b"\r\n")
                
                # 旁路观察：行缓冲提取 usage
                sse_buf.feed(chunk)
            
            sse_buf.flush()  # 处理残余
            handler._safe_write(b"0\r\n\r\n")
            
            # 保存提取的 usage
            handler._usage = sse_buf.usage  # NEW
        except Exception as e:
            log.debug("Stream forwarding interrupted: %s", e)
```

### 2.2 `handle_chat` 日志记录

```python
# 流式请求日志（当前 tokens_in/tokens_out 为 0）
pm.logger.log(RequestLog(
    req_id=req_id, key_name=key_name, model=model,
    status=200, ttft_ms=ttft, route="local",
    tokens_in=getattr(handler, '_usage', {}).get("prompt_tokens", 0),
    tokens_out=getattr(handler, '_usage', {}).get("completion_tokens", 0),
    duration_ms=(time.monotonic()-handler._req_start)*1000,
))
```

## Phase 3: Ollama Native 流式路径

Ollama 流式响应不返回 `eval_count`/`prompt_eval_count`。两个选项：

**选项 A（推荐）**: 流结束时发一个额外的非流式请求获取统计 → 延迟增加，不值得
**选项 B（采用）**: Ollama 流式不提取 usage，tokens 保持 0 → 与当前行为一致，不引入回归

Ollama native 流已有行解析逻辑，但 JSON 对象中不含 usage 字段。维持现状。

## Phase 4: 初始化 handler._usage

在 `handle_chat` 入口处：

```python
handler._usage = {"prompt_tokens": 0, "completion_tokens": 0}
```

确保非流式路径和流式路径都能安全访问。

## Phase 5: 测试

### 5.1 单元测试 `tests/test_g1b_streaming_usage.py`

| 测试 | 描述 |
|------|------|
| `test_single_chunk_usage` | 单个 chunk 含 usage → 提取成功 |
| `test_split_usage_chunk` | usage chunk 跨 8192 边界拆成两块 → 提取成功 |
| `test_no_usage` | 流无 usage → tokens 保持 0 |
| `test_multiple_usage_updates` | 多个 chunk 含 usage（vLLM 逐步更新）→ 最后值胜出 |
| `test_done_marker` | `data: [DONE]` 后不再解析 |
| `test_comment_lines` | SSE 注释行 `: ping` 被跳过 |
| `test_malformed_json` | 损坏 JSON 静默跳过 |
| `test_buffer_residual` | 流结束时 buffer 有残余 → flush 提取 |
| `test_non_sse_content_type` | content-type≠text/event-stream → 不解析（未来保护） |
