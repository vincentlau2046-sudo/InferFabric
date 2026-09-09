"""G-1b: Streaming Usage Extraction 测试

覆盖 SSELineBuffer 的行缓冲、usage 提取、边界条件。
"""

import json
import pytest

from inferfabric.proxy.sse_buffer import SSELineBuffer


def _sse_chunk(obj: dict) -> bytes:
    """构造单个 SSE data chunk（含 \\n\\n 终止符）。"""
    return f"data: {json.dumps(obj)}\n\n".encode()


def _sse_done() -> bytes:
    """构造 SSE [DONE] 标记。"""
    return b"data: [DONE]\n\n"


def _usage_chunk(prompt_tokens: int = 10, completion_tokens: int = 20,
                 model: str = "qwen36-27b") -> dict:
    """构造含 usage 的 SSE chunk 对象。"""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _content_chunk(content: str = "hello", model: str = "qwen36-27b") -> dict:
    """构造普通 content SSE chunk（无 usage）。"""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


class TestSSELineBuffer:
    def test_single_chunk_usage(self):
        """单个 chunk 含 usage → 提取成功。"""
        buf = SSELineBuffer()
        buf.feed(_sse_chunk(_usage_chunk(100, 200)))
        buf.flush()
        assert buf.usage["prompt_tokens"] == 100
        assert buf.usage["completion_tokens"] == 200

    def test_split_usage_chunk(self):
        """usage chunk 跨 8192 边界拆成两块 → 提取成功。"""
        buf = SSELineBuffer()
        full = _sse_chunk(_usage_chunk(50, 75)) + _sse_done()
        mid = len(full) // 2
        buf.feed(full[:mid])
        buf.feed(full[mid:])
        buf.flush()
        assert buf.usage["prompt_tokens"] == 50
        assert buf.usage["completion_tokens"] == 75

    def test_no_usage(self):
        """流无 usage → tokens 保持 0。"""
        buf = SSELineBuffer()
        buf.feed(_sse_chunk(_content_chunk("hi")))
        buf.feed(_sse_done())
        buf.flush()
        assert buf.usage["prompt_tokens"] == 0
        assert buf.usage["completion_tokens"] == 0

    def test_multiple_usage_updates(self):
        """多个 chunk 含 usage → 最后值胜出。"""
        buf = SSELineBuffer()
        buf.feed(_sse_chunk(_usage_chunk(10, 5)))
        buf.feed(_sse_chunk(_usage_chunk(15, 10)))
        buf.feed(_sse_chunk(_usage_chunk(20, 25)))
        buf.feed(_sse_done())
        buf.flush()
        assert buf.usage["prompt_tokens"] == 20
        assert buf.usage["completion_tokens"] == 25

    def test_done_marker(self):
        """data: [DONE] 不触发解析。"""
        buf = SSELineBuffer()
        buf.feed(_sse_chunk(_usage_chunk(10, 20)))
        buf.feed(_sse_done())
        buf.flush()
        assert buf.usage["prompt_tokens"] == 10

    def test_comment_lines(self):
        """SSE 注释行 `: ping` 被跳过。"""
        buf = SSELineBuffer()
        event = b": ping\n\ndata: " + json.dumps(_usage_chunk(5, 8)).encode() + b"\n\n"
        buf.feed(event)
        buf.flush()
        assert buf.usage["prompt_tokens"] == 5
        assert buf.usage["completion_tokens"] == 8

    def test_malformed_json(self):
        """损坏 JSON 静默跳过，不影响后续解析。"""
        buf = SSELineBuffer()
        buf.feed(b"data: {invalid json}\n\n")
        buf.feed(_sse_chunk(_usage_chunk(30, 40)))
        buf.flush()
        assert buf.usage["prompt_tokens"] == 30

    def test_buffer_residual(self):
        """流结束时 buffer 有残余（无 \\n\\n）→ flush 提取。"""
        buf = SSELineBuffer()
        # 先喂一个完整事件
        buf.feed(_sse_chunk(_content_chunk("first")))
        # 再喂一个不完整的事件
        buf.feed(b"data: " + json.dumps(_usage_chunk(7, 9)).encode())
        # 没有 \n\n 终止
        buf.flush()
        assert buf.usage["prompt_tokens"] == 7
        assert buf.usage["completion_tokens"] == 9

    def test_flush_clears_buffer(self):
        """flush 后 buffer 为空，不影响后续 feed。"""
        buf = SSELineBuffer()
        buf.feed(_sse_chunk(_usage_chunk(10, 20)))
        buf.flush()
        # 再 feed 新数据
        buf.feed(_sse_chunk(_usage_chunk(30, 40)))
        buf.flush()
        assert buf.usage["prompt_tokens"] == 30

    def test_cRLF_normalization(self):
        """CRLF (\\r\\n\\r\\n) 事件终止符被规范化。"""
        buf = SSELineBuffer()
        usage_obj = _usage_chunk(42, 99)
        event = f"data: {json.dumps(usage_obj)}\r\n\r\n".encode()
        buf.feed(event)
        buf.flush()
        assert buf.usage["prompt_tokens"] == 42
        assert buf.usage["completion_tokens"] == 99

    def test_data_no_space_after_colon(self):
        """data:后无空格也能解析。"""
        buf = SSELineBuffer()
        usage_obj = _usage_chunk(15, 25)
        event = b"data:" + json.dumps(usage_obj).encode() + b"\n\n"
        buf.feed(event)
        buf.flush()
        assert buf.usage["prompt_tokens"] == 15

    def test_data_double_space_after_colon(self):
        """data:  后有双空格也能解析（lstrip）。"""
        buf = SSELineBuffer()
        usage_obj = _usage_chunk(15, 25)
        event = b"data:  " + json.dumps(usage_obj).encode() + b"\n\n"
        buf.feed(event)
        buf.flush()
        assert buf.usage["prompt_tokens"] == 15

    def test_zero_tokens_usage_skipped(self):
        """usage 中 prompt_tokens=0, completion_tokens=0 → 不更新（保持旧值）。"""
        buf = SSELineBuffer()
        buf.feed(_sse_chunk(_usage_chunk(10, 20)))
        # 发一个零值 usage
        zero_usage = _usage_chunk(0, 0)
        buf.feed(_sse_chunk(zero_usage))
        buf.flush()
        # 零值不更新，保持 10/20
        assert buf.usage["prompt_tokens"] == 10
        assert buf.usage["completion_tokens"] == 20

    def test_real_vllm_stream_sequence(self):
        """模拟真实 vLLM 流式输出序列：多个 content chunk → usage chunk → [DONE]。"""
        buf = SSELineBuffer()
        # Content chunks
        for word in ["Hello", " world", "!", " How", " can", " I", " help?"]:
            buf.feed(_sse_chunk(_content_chunk(word)))
        # Final usage chunk
        buf.feed(_sse_chunk(_usage_chunk(15, 7)))
        # DONE
        buf.feed(_sse_done())
        buf.flush()
        assert buf.usage["prompt_tokens"] == 15
        assert buf.usage["completion_tokens"] == 7

    def test_anthropic_message_start_nested_usage(self):
        """Anthropic message_start：usage 嵌套在 message 下（input_tokens）。"""
        buf = SSELineBuffer()
        event = b"event: message_start\ndata: " + json.dumps({
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "model": "qwen38-27b-abliterated",
                "usage": {"input_tokens": 105},
            },
        }).encode() + b"\n\n"
        buf.feed(event)
        buf.flush()
        assert buf.usage["prompt_tokens"] == 105
        assert buf.usage["completion_tokens"] == 0

    def test_anthropic_stream_full_sequence(self):
        """Anthropic 完整流式序列：message_start(input) → content_block_delta × N → message_delta(output)。

        验证按字段合并：message_delta 的 usage 只有 output_tokens，
        不会把 message_start 已提取的 input_tokens 清零。
        """
        buf = SSELineBuffer()
        # message_start（含嵌套 message.usage.input_tokens）
        buf.feed(b"event: message_start\ndata: " + json.dumps({
            "type": "message_start",
            "message": {"id": "msg_x", "usage": {"input_tokens": 105}},
        }).encode() + b"\n\n")
        # content_block_delta（无 usage）
        for i in range(3):
            buf.feed(b"event: content_block_delta\ndata: " + json.dumps({
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": f"word{i} "},
            }).encode() + b"\n\n")
        # message_delta（只有 output_tokens）
        buf.feed(b"event: message_delta\ndata: " + json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": "stop"},
            "usage": {"output_tokens": 70},
        }).encode() + b"\n\n")
        # message_stop
        buf.feed(b"event: message_stop\ndata: " + json.dumps({"type": "message_stop"}).encode() + b"\n\n")
        buf.flush()
        assert buf.usage["prompt_tokens"] == 105
        assert buf.usage["completion_tokens"] == 70
