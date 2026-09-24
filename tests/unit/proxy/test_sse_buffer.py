"""
unit/proxy/test_sse_buffer.py — SSE 流式 Usage 提取测试

测试对象: inferfabric.proxy.sse_buffer.SSELineBuffer
覆盖范围:
  - 单 chunk / 跨边界拆分 / 无 usage / 多 usage 取最后值
  - [DONE] 标记、注释行、损坏 JSON、残余缓冲
  - CRLF 规范化、data: 后无空格 / 双空格
  - 零值 usage 跳过、真实 vLLM 流式序列
  - Anthropic message_start / message_delta 嵌套 usage
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


class TestSSELineBufferCacheFields:
    """缓存命中统计：总输入 = 新增 + cache_read（OpenAI 系 cached 为子集不重复加）。"""

    def test_anthropic_stream_with_cache_hit(self):
        """Anthropic 流式 + 缓存命中（ninfer 真实场景）：总量补回 cache_read。"""
        buf = SSELineBuffer()
        buf.feed(b"event: message_start\ndata: " + json.dumps({
            "type": "message_start",
            "message": {"id": "msg_x", "usage": {
                "input_tokens": 16086,
                "output_tokens": 1,
                "cache_read_input_tokens": 129428,
            }},
        }).encode() + b"\n\n")
        buf.feed(b"event: message_delta\ndata: " + json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": "stop"},
            "usage": {"input_tokens": 16086, "output_tokens": 2112},
        }).encode() + b"\n\n")
        buf.flush()
        assert buf.usage["prompt_tokens"] == 145514      # 16086 + 129428
        assert buf.usage["prompt_tokens_cached"] == 129428
        assert buf.usage["completion_tokens"] == 2112

    def test_openai_stream_with_cached_details(self):
        """OpenAI 流式：prompt_tokens 已含缓存，cached 取 details 子集。"""
        obj = _usage_chunk(1000, 50)
        obj["usage"]["prompt_tokens_details"] = {"cached_tokens": 800}
        buf = SSELineBuffer()
        buf.feed(_sse_chunk(obj))
        buf.flush()
        assert buf.usage["prompt_tokens"] == 1000
        assert buf.usage["prompt_tokens_cached"] == 800

    def test_no_usage_has_zero_cached_key(self):
        """无 usage 流 → 三个键全 0（含新增的 prompt_tokens_cached）。"""
        buf = SSELineBuffer()
        buf.feed(_sse_chunk(_content_chunk("hi")))
        buf.flush()
        assert buf.usage["prompt_tokens"] == 0
        assert buf.usage["prompt_tokens_cached"] == 0
        assert buf.usage["completion_tokens"] == 0

    def test_zero_usage_chunk_keeps_cached(self):
        """零值 usage 事件不清空已提取的缓存命中。"""
        obj = _usage_chunk(1000, 50)
        obj["usage"]["prompt_tokens_details"] = {"cached_tokens": 800}
        buf = SSELineBuffer()
        buf.feed(_sse_chunk(obj))
        buf.feed(_sse_chunk(_usage_chunk(0, 0)))
        buf.flush()
        assert buf.usage["prompt_tokens"] == 1000
        assert buf.usage["prompt_tokens_cached"] == 800


class TestFirstTokenDetection:
    """v6.1: 首 token 检测（first_token_seen + on_first_token 回调）。

    判据：首个「带内容 delta」事件触发一次 —
    OpenAI choices[0].delta.content / reasoning_content、
    Anthropic delta.text / thinking、顶层 delta.content；
    role-only / message_start / usage-only / [DONE] 均不触发。
    """

    def test_openai_role_preamble_does_not_trigger(self):
        """role-only 前言 chunk 不触发（TTFT 不应记在首个 chunk 上）。"""
        calls = []
        buf = SSELineBuffer(calls.append)
        buf.feed(_sse_chunk({"choices": [{"delta": {"role": "assistant"}}]}))
        assert not buf.first_token_seen
        assert calls == []

    def test_openai_delta_content_triggers_once(self):
        """首个 content delta 触发一次；后续 content 不再触发。"""
        calls = []
        buf = SSELineBuffer(calls.append)
        buf.feed(_sse_chunk({"choices": [{"delta": {"role": "assistant"}}]}))
        buf.feed(_sse_chunk(_content_chunk("hi")))
        assert buf.first_token_seen
        assert len(calls) == 1
        assert isinstance(calls[0], float)
        buf.feed(_sse_chunk(_content_chunk(" there")))
        assert len(calls) == 1

    def test_openai_reasoning_content_triggers(self):
        """reasoning_content（thinking 模型先流式推理）也算首个内容 token。"""
        calls = []
        buf = SSELineBuffer(calls.append)
        buf.feed(_sse_chunk({"choices": [{"delta": {"reasoning_content": "..."}}]}))
        assert buf.first_token_seen and len(calls) == 1

    def test_top_level_delta_content_triggers(self):
        """顶层 delta.content（非 choices 包裹形态）也识别。"""
        calls = []
        buf = SSELineBuffer(calls.append)
        buf.feed(b'data: {"delta": {"content": "hi"}}\n\n')
        assert buf.first_token_seen and len(calls) == 1

    def test_anthropic_text_delta_triggers(self):
        """Anthropic message_start 是前言；content_block_delta 才触发。"""
        calls = []
        buf = SSELineBuffer(calls.append)
        buf.feed(b"event: message_start\ndata: " + json.dumps({
            "type": "message_start",
            "message": {"id": "m", "usage": {"input_tokens": 10}},
        }).encode() + b"\n\n")
        assert not buf.first_token_seen
        buf.feed(b"event: content_block_delta\ndata: " + json.dumps({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hi"},
        }).encode() + b"\n\n")
        assert buf.first_token_seen and len(calls) == 1

    def test_usage_only_and_done_do_not_trigger(self):
        """usage-only 事件（空 delta）+ [DONE] 不触发。"""
        calls = []
        buf = SSELineBuffer(calls.append)
        buf.feed(_sse_chunk(_usage_chunk(10, 20)))
        buf.feed(_sse_done())
        buf.flush()
        assert not buf.first_token_seen
        assert calls == []

    def test_no_callback_only_tracks_flag(self):
        """不传回调：仅维护 first_token_seen，usage 行为不变。"""
        buf = SSELineBuffer()
        buf.feed(_sse_chunk(_content_chunk("hi")))
        buf.feed(_sse_chunk(_usage_chunk(15, 7)))
        buf.feed(_sse_done())
        buf.flush()
        assert buf.first_token_seen is True
        assert buf.usage["prompt_tokens"] == 15
        assert buf.usage["completion_tokens"] == 7
