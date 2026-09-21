"""SSE 流式行缓冲 + usage 提取器

G-1b: 在流式转发路径中旁路观察 SSE 事件，提取 usage 字段。
纯观察者：不修改转发路径，零延迟透传。
线程不安全（每个请求独占一个实例）。

生命周期: feed() × N → flush() → .usage
"""

import json
import logging

from inferfabric.proxy.usage import normalize_usage

log = logging.getLogger("inferfabric.sse_buffer")


class SSELineBuffer:
    """SSE 流式行缓冲 + usage 提取器。

    用法:
        buf = SSELineBuffer()
        while chunk := resp.read(8192):
            # 1. 立即转发（零延迟）
            handler._safe_write(chunk)
            # 2. 旁路观察
            buf.feed(chunk)
        buf.flush()
        usage = buf.usage  # {"prompt_tokens", "prompt_tokens_cached", "completion_tokens"}
    """

    __slots__ = ("_buffer", "usage")

    def __init__(self):
        self._buffer = b""
        self.usage = {"prompt_tokens": 0, "prompt_tokens_cached": 0,
                      "completion_tokens": 0}

    def feed(self, chunk: bytes):
        """喂入原始 chunk。在 resp.read() → _safe_write() 之间调用。

        解析完整 SSE 事件（以 \\n\\n 分隔），提取 usage。
        不完整的事件留在 buffer 中等待下次 feed。
        """
        # CRLF 规范化：防御性处理 \r\n\r\n 事件终止符
        self._buffer += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in self._buffer:
            event_bytes, self._buffer = self._buffer.split(b"\n\n", 1)
            self._parse_event(event_bytes)

    def flush(self):
        """流结束，处理 buffer 中的残余数据。无条件清空 buffer。"""
        if self._buffer.strip():
            self._parse_event(self._buffer)
        self._buffer = b""

    def _parse_event(self, event_bytes: bytes):
        """解析单个 SSE 事件，提取 usage（如果存在）。

        SSE 事件格式:
            data: {"id":"...","choices":[...],"usage":{...}}\\n
            \\n

        也可能有多行 data:（但 vLLM/OpenAI 每个事件只有一行 data:）。
        """
        for line in event_bytes.split(b"\n"):
            line = line.strip()
            if not line or line.startswith(b":"):
                continue

            # 提取 data: 前缀后的内容
            if line.startswith(b"data:"):
                data_str = line[5:].lstrip()  # data: 或 data: 后的空格
            else:
                continue

            # [DONE] 标记 — 跳过不解析
            if data_str.strip() == b"[DONE]":
                continue

            # 解析 JSON
            try:
                obj = json.loads(data_str)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            # 提取 usage（按字段合并：各字段独立 last-wins，零值不覆盖）
            usage = obj.get("usage")
            if not usage:
                # Anthropic message_start 事件：usage 嵌套在 message 下
                msg = obj.get("message")
                if isinstance(msg, dict):
                    usage = msg.get("usage")
            if usage and isinstance(usage, dict):
                # 双协议归一化（OpenAI prompt_tokens 含缓存 / Anthropic 补回
                # cache_read+cache_creation），口径统一见 proxy/usage.py。
                # 按字段取 max：单请求内 token 计数单调不减；Anthropic 流中
                # message_start 带 cache_read、message_delta 常只报 input+output
                # （last-wins 会把总量打回未含缓存的小值），max 保真。
                # 零值事件自然不覆盖（max(x, 0) = x）。
                n = normalize_usage(usage)
                if n["prompt_tokens"]:
                    self.usage["prompt_tokens"] = max(
                        self.usage["prompt_tokens"], n["prompt_tokens"])
                    self.usage["prompt_tokens_cached"] = max(
                        self.usage["prompt_tokens_cached"],
                        n["prompt_tokens_cached"])
                if n["completion_tokens"]:
                    self.usage["completion_tokens"] = max(
                        self.usage["completion_tokens"], n["completion_tokens"])
