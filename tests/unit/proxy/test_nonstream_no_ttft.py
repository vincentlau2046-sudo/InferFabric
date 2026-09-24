"""unit/proxy/test_nonstream_no_ttft.py — 非流式请求不记 TTFT（TPOT 零值根治）

非流式：引擎算完整响应才回 header，"收到 header" ≈ 总耗时，没有"首 token"语义。
若记为 ttft_ms，aggregator 派生 TPOT = (duration - ttft)/(tokens_out-1) ≈ 0.001
→ 四舍五入 0.0，污染模型延迟趋势（0ms 值）。

契约：非流式成功路径不设置 handler._ttft_ms（保持 None → 请求日志 ttft_ms NULL）；
流式路径回归守卫 —— v6.1 起 TTFT 记在「首个内容 delta」（SSELineBuffer 首 token
回调），而非首个 chunk（role-only / message_start 前言 chunk 不算 token）。
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import inferfabric.proxy.chat_handlers as chat_handlers


class _FakeResp:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.status = 200

    def getheaders(self):
        return [("Content-Type", "application/json")]

    def read(self, n=None):
        if n is None:
            data = b"".join(self._chunks)
            self._chunks = []
            return data
        if not self._chunks:
            return b""
        data, self._chunks = self._chunks[0], self._chunks[1:]
        return data

    def close(self):
        pass


class _FakeConn:
    def __init__(self, resp):
        self._resp = resp

    def request(self, *a, **k):
        pass

    def getresponse(self):
        return self._resp

    def close(self):
        pass


class _H:
    """FakeHandler 最小集：_forward_request 用到的 handler 接口。"""
    def __init__(self):
        self.path = "/v1/chat/completions"
        self._req_start = time.monotonic()
        self._sent = []

    def send_response(self, code):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    def _safe_write(self, b):
        self._sent.append(b)


def _pm(resp):
    pm = SimpleNamespace()
    pm.make_conn = lambda port: _FakeConn(resp)
    pm.release_port = lambda *a: None
    return pm


def test_nonstream_success_does_not_record_ttft():
    """非流式成功：ttft 保持 None（不产生 (duration-ttft)≈0 的零值 TPOT 样本）。"""
    body = b'{"choices": [{"message": {"content": "hi"}}], "usage": {"completion_tokens": 10, "prompt_tokens": 5}}'
    h = _H()
    ok = chat_handlers._forward_request(h, _pm(_FakeResp([body])), 8003, body, False, model_name="m")
    assert ok is True
    assert getattr(h, "_ttft_ms", None) is None


def test_stream_still_records_ttft():
    """回归守卫：流式路径仍记录 TTFT（v6.1 起 = 首个内容 delta 时刻）。"""
    chunks = [b'data: {"delta": {"content": "hi"}}\n\n', b"data: [DONE]\n\n"]
    h = _H()
    ok = chat_handlers._forward_request(h, _pm(_FakeResp(chunks)), 8003, b"{}", True, model_name="m")
    assert ok is True
    assert h._ttft_ms is not None and h._ttft_ms >= 0


def test_stream_ttft_at_first_content_not_first_chunk():
    """v6.1: TTFT 落在首个内容 delta，不在首个（role-only 前言）chunk。"""
    role_chunk = b'data: {"choices": [{"delta": {"role": "assistant"}}]}\n\n'
    content_chunk = b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
    chunks = [role_chunk, content_chunk, b"data: [DONE]\n\n"]
    h = _H()
    ok = chat_handlers._forward_request(h, _pm(_FakeResp(chunks)), 8003, b"{}", True, model_name="m")
    assert ok is True
    # 回调在 content chunk 解析时触发 → _ttft_ms 被设置（值 ≥ 0，距 _req_start 很小）
    assert h._ttft_ms is not None and h._ttft_ms >= 0


def test_stream_without_content_keeps_ttft_none():
    """流里只有 role/usage 前言、无内容 delta → 不记 TTFT（保持 None）。"""
    role_chunk = b'data: {"choices": [{"delta": {"role": "assistant"}}]}\n\n'
    usage_chunk = (b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], '
                   b'"usage": {"prompt_tokens": 5, "completion_tokens": 0}}\n\n')
    h = _H()
    ok = chat_handlers._forward_request(h, _pm(_FakeResp([role_chunk, usage_chunk, b"data: [DONE]\n\n"])),
                                        8003, b"{}", True, model_name="m")
    assert ok is True
    assert getattr(h, "_ttft_ms", None) is None
