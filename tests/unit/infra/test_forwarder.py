"""
unit/infra/test_forwarder.py — 转发器单元测试

测试对象: inferfabric.forwarder
覆盖范围:
  - send_json: 基本 JSON 发送、CORS 头、extra_headers、BrokenPipe 容错
  - read_body: 有效 JSON、空 body、超大 payload (413)、无效 JSON (400)
  - handle_json_response: 成功 200、错误 502、模型返回无效 JSON、缓存写
  - CloudResult: 默认值、完整构造
  - forward_to_cloud: 协议不可用 (501)、base 缺失 (501)
  - forward_anthropic_local: 连接失败 → 503、重试调用 exponential_backoff
  - pipe_stream_response: chunked 传输、TTFT 记录
"""

import json
import time
from io import BytesIO
from unittest.mock import MagicMock, patch, PropertyMock, call

import pytest


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _make_handler():
    """Create a properly mocked HTTP handler for forwarder functions."""
    h = MagicMock()
    h.wfile = MagicMock()
    h.wfile.write = MagicMock()
    h.wfile.flush = MagicMock()
    h.rfile = MagicMock()
    h.headers = {}
    h.send_response = MagicMock()
    h.send_header = MagicMock()
    h.end_headers = MagicMock()
    return h


# ═══════════════════════════════════════════════════════════════
# 1. send_json
# ═══════════════════════════════════════════════════════════════


class TestSendJson:

    def test_sends_valid_json(self):
        """send_json 发送有效的 JSON 响应。"""
        from inferfabric.forwarder import send_json
        h = _make_handler()
        send_json(h, {"ok": True})

        h.send_response.assert_called_once()
        h.end_headers.assert_called_once()
        h.wfile.flush.assert_called_once()

    def test_sends_cors_headers(self):
        """send_json 发送 CORS 头。"""
        from inferfabric.forwarder import send_json
        h = _make_handler()
        send_json(h, {})

        headers = [c[0][0] for c in h.send_header.call_args_list]
        assert "Access-Control-Allow-Origin" in headers
        assert "Content-Type" in headers

    def test_sends_extra_headers(self):
        """send_json 发送自定义 extra_headers（跳过 None）。"""
        from inferfabric.forwarder import send_json
        h = _make_handler()
        send_json(h, {}, extra_headers={"X-Custom": "yes", "X-Skip": None})

        headers_sent = {c[0][0]: c[0][1] for c in h.send_header.call_args_list}
        assert headers_sent["X-Custom"] == "yes"
        assert "X-Skip" not in headers_sent

    def test_survives_broken_pipe(self):
        """客户端断开连接时不抛异常。"""
        from inferfabric.forwarder import send_json
        h = _make_handler()
        h.wfile.write.side_effect = BrokenPipeError()

        # 不应该抛异常
        send_json(h, {"ok": True})


# ═══════════════════════════════════════════════════════════════
# 2. read_body
# ═══════════════════════════════════════════════════════════════


class TestReadBody:

    def test_reads_valid_json(self):
        """read_body 解析有效 JSON。"""
        from inferfabric.forwarder import read_body
        h = _make_handler()
        body = json.dumps({"model": "test", "messages": []}).encode()
        h.headers["Content-Length"] = str(len(body))
        h.rfile.read.return_value = body

        result = read_body(h)
        assert result == {"model": "test", "messages": []}

    def test_empty_body_returns_empty_dict(self):
        """Content-Length=0 返回 {}。"""
        from inferfabric.forwarder import read_body
        h = _make_handler()
        h.headers["Content-Length"] = "0"

        result = read_body(h)
        assert result == {}

    @patch("inferfabric.forwarder.send_json")
    def test_payload_too_large(self, mock_send):
        """超过 100MB 返回 None 并发送 413。"""
        from inferfabric.forwarder import read_body
        h = _make_handler()
        h.headers["Content-Length"] = str(101 * 1024 * 1024)  # 101MB

        result = read_body(h)
        assert result is None
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert "payload" in args[1]["error"].lower()
        assert args[2] == 413

    @patch("inferfabric.forwarder.send_json")
    def test_invalid_json(self, mock_send):
        """无效 JSON 返回 None 并发送 400。"""
        from inferfabric.forwarder import read_body
        h = _make_handler()
        body = b"not json"
        h.headers["Content-Length"] = str(len(body))
        h.rfile.read.return_value = body

        result = read_body(h)
        assert result is None
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[2] == 400


# ═══════════════════════════════════════════════════════════════
# 3. handle_json_response
# ═══════════════════════════════════════════════════════════════


class TestHandleJsonResponse:

    @patch("inferfabric.forwarder.send_json")
    def test_success_with_usage(self, mock_send):
        """status=200 时发送结果并设置 handler._usage。"""
        from inferfabric.forwarder import handle_json_response
        h = _make_handler()
        h._usage = None  # pre-set so MagicMock doesn't interfere
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }).encode()
        model = MagicMock()
        model.name = "test"

        handle_json_response(h, resp, model, "test-model", {}, "auth")
        assert h._usage["prompt_tokens"] == 10
        assert h._usage["completion_tokens"] == 5

    @patch("inferfabric.forwarder.send_json")
    def test_error_status_returns_502(self, mock_send):
        """非 200 状态返回 502 error。"""
        from inferfabric.forwarder import handle_json_response
        h = _make_handler()
        resp = MagicMock()
        resp.status = 500
        resp.read.return_value = b"Internal error"
        model = MagicMock()
        model.name = "test"

        handle_json_response(h, resp, model, "test-model", {}, "auth")
        # send_json(handler, error_dict, 502)
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[2] == 502
        assert "500" in args[1]["error"]

    @patch("inferfabric.forwarder.send_json")
    def test_invalid_json_from_model(self, mock_send):
        """模型返回非 JSON 时返回 502。"""
        from inferfabric.forwarder import handle_json_response
        h = _make_handler()
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = b"plain text, not json"
        model = MagicMock()
        model.name = "test"

        handle_json_response(h, resp, model, "test-model", {}, "auth")
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert "invalid" in args[1]["error"].lower()

    @patch("inferfabric.forwarder.send_json")
    def test_cache_put_on_success(self, mock_send):
        """成功的非流式响应写入缓存。"""
        from inferfabric.forwarder import handle_json_response
        h = _make_handler()
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps({
            "choices": [{}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()
        model = MagicMock()
        model.name = "test"
        cache = MagicMock()

        handle_json_response(h, resp, model, "test-model",
                             {"model": "test"}, "auth",
                             response_cache=cache)
        cache.put.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# 4. CloudResult
# ═══════════════════════════════════════════════════════════════


class TestCloudResult:

    def test_defaults(self):
        """CloudResult 有合理默认值。"""
        from inferfabric.forwarder import CloudResult
        cr = CloudResult()
        assert cr.status == 200
        assert cr.usage == {}
        assert cr.ttft_ms is None
        assert cr.duration_ms == 0.0
        assert cr.error is None

    def test_full_fields(self):
        """CloudResult 完整字段构造。"""
        from inferfabric.forwarder import CloudResult
        cr = CloudResult(
            status=502, usage={"prompt_tokens": 100},
            ttft_ms=50.0, duration_ms=200.0, error="timeout",
        )
        assert cr.status == 502
        assert cr.usage["prompt_tokens"] == 100
        assert cr.ttft_ms == 50.0
        assert cr.error == "timeout"


# ═══════════════════════════════════════════════════════════════
# 5. forward_to_cloud — 协议校验
# ═══════════════════════════════════════════════════════════════


class TestForwardToCloud:

    @patch("inferfabric.forwarder.send_json")
    def test_anthropic_not_available(self, mock_send):
        """Anthropic 协议不可用 → 501。"""
        from inferfabric.forwarder import forward_to_cloud
        h = _make_handler()
        provider = MagicMock()
        provider.name = "test"
        provider.anthropic_base = "https://api.test.com"

        cloud = MagicMock()
        cloud.anthropic_available = False
        cloud.openai_available = True
        cloud.model_id = "m"

        result = forward_to_cloud(h, {}, provider, cloud, protocol="anthropic")
        assert result.status == 501
        assert "Anthropic" in result.error

    @patch("inferfabric.forwarder.send_json")
    def test_openai_not_available(self, mock_send):
        """OpenAI 协议不可用 → 501。"""
        from inferfabric.forwarder import forward_to_cloud
        h = _make_handler()
        provider = MagicMock()
        provider.name = "test"
        provider.openai_base = "https://api.test.com"

        cloud = MagicMock()
        cloud.anthropic_available = True
        cloud.openai_available = False
        cloud.model_id = "m"

        result = forward_to_cloud(h, {}, provider, cloud, protocol="openai")
        assert result.status == 501
        assert "OpenAI" in result.error

    @patch("inferfabric.forwarder.send_json")
    def test_missing_anthropic_base(self, mock_send):
        """Anthropic base 缺失 → 501。"""
        from inferfabric.forwarder import forward_to_cloud
        h = _make_handler()
        provider = MagicMock()
        provider.name = "test"
        provider.anthropic_base = ""

        cloud = MagicMock()
        cloud.anthropic_available = True
        cloud.model_id = "m"

        result = forward_to_cloud(h, {}, provider, cloud, protocol="anthropic")
        assert result.status == 501

    @patch("inferfabric.forwarder.send_json")
    def test_501_body_includes_hint_openai_only_provider(self, mock_send):
        """Anthropic 请求打到 OpenAI-only provider → 501 错误体含 hint，引导改用 OpenAI 端点。"""
        from inferfabric.forwarder import forward_to_cloud
        h = _make_handler()
        provider = MagicMock()
        provider.name = "agnes"
        provider.openai_base = "https://agnes.example.com/v1"
        provider.anthropic_base = ""
        cloud = MagicMock()
        cloud.anthropic_available = False
        cloud.openai_available = True
        cloud.model_id = "agnes-1"

        result = forward_to_cloud(h, {}, provider, cloud, protocol="anthropic")
        assert result.status == 501
        # send_json(handler, body, status) → call_args[0][1] 是错误体 dict
        body = mock_send.call_args[0][1]
        assert "hint" in body
        assert "/v1/chat/completions" in body["hint"]

    @patch("inferfabric.forwarder.send_json")
    def test_501_body_includes_hint_anthropic_only_provider(self, mock_send):
        """OpenAI 请求打到 Anthropic-only provider → 501 错误体含 hint，引导改用 Anthropic 端点。"""
        from inferfabric.forwarder import forward_to_cloud
        h = _make_handler()
        provider = MagicMock()
        provider.name = "anthropic-only"
        provider.openai_base = ""
        provider.anthropic_base = "https://anthropic.example.com"
        cloud = MagicMock()
        cloud.anthropic_available = True
        cloud.openai_available = False
        cloud.model_id = "m"

        result = forward_to_cloud(h, {}, provider, cloud, protocol="openai")
        assert result.status == 501
        body = mock_send.call_args[0][1]
        assert "hint" in body
        assert "/v1/messages" in body["hint"]

    @patch("inferfabric.forwarder.send_json")
    def test_501_body_includes_hint_missing_base(self, mock_send):
        """base 缺失的 501 也带 hint（提示配置问题，而非引导换协议端点）。"""
        from inferfabric.forwarder import forward_to_cloud
        h = _make_handler()
        provider = MagicMock()
        provider.name = "test"
        provider.anthropic_base = ""
        cloud = MagicMock()
        cloud.anthropic_available = True
        cloud.model_id = "m"

        result = forward_to_cloud(h, {}, provider, cloud, protocol="anthropic")
        assert result.status == 501
        body = mock_send.call_args[0][1]
        assert "hint" in body

    @patch("inferfabric.forwarder.urlopen")
    @patch("inferfabric.forwarder.send_json")
    def test_cloud_nonstream_ttft_is_none(self, mock_send, mock_urlopen):
        """v6.1: 云端非流式成功 → ttft_ms=None（header 到达无首 token 语义，
        与本地非流式路径一致 → 请求日志 ttft_ms NULL，不进 TPOT 分位）。"""
        from inferfabric.forwarder import forward_to_cloud
        h = _make_handler()
        provider = MagicMock()
        provider.name = "test"
        provider.openai_base = "https://api.test.com/v1"
        provider.timeout = 5
        cloud = MagicMock()
        cloud.anthropic_available = True
        cloud.openai_available = True
        cloud.model_id = "m-1"
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps(
            {"choices": [{}], "usage": {"prompt_tokens": 5, "completion_tokens": 10}}
        ).encode()
        mock_urlopen.return_value = resp

        result = forward_to_cloud(h, {"model": "m", "stream": False},
                                  provider, cloud, protocol="openai")
        assert result.status == 200
        assert result.ttft_ms is None
        assert result.duration_ms >= 0.0
        assert result.usage["completion_tokens"] == 10

    @patch("inferfabric.forwarder.urlopen")
    @patch("inferfabric.forwarder.send_json")
    def test_cloud_stream_ttft_from_first_token(self, mock_send, mock_urlopen):
        """v6.1: 云端流式 → ttft_ms 取自首个内容 token（回调记 handler._ttft_ms），
        不再用 urlopen 返回（header 到达）时刻。"""
        from inferfabric.forwarder import forward_to_cloud
        h = _make_handler()
        h._req_start = time.monotonic()
        provider = MagicMock()
        provider.name = "test"
        provider.openai_base = "https://api.test.com/v1"
        provider.timeout = 5
        cloud = MagicMock()
        cloud.anthropic_available = True
        cloud.openai_available = True
        cloud.model_id = "m-1"
        resp = MagicMock()
        resp.status = 200
        resp.getheader.return_value = None
        content = b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
        resp.read.side_effect = [content, b""]
        mock_urlopen.return_value = resp

        result = forward_to_cloud(h, {"model": "m", "stream": True},
                                  provider, cloud, protocol="openai")
        assert result.status == 200
        assert h._ttft_ms is not None          # 回调在首个 content delta 时设置
        assert result.ttft_ms == h._ttft_ms    # CloudResult 直接透传该值


# ═══════════════════════════════════════════════════════════════
# 6. pipe_stream_response
# ═══════════════════════════════════════════════════════════════


class TestPipeStreamResponse:

    def test_chunked_transfer_encoding(self):
        """pipe_stream 发送 chunked Transfer-Encoding。"""
        from inferfabric.forwarder import pipe_stream_response
        h = _make_handler()
        h._req_start = time.monotonic()

        resp = MagicMock()
        resp.status = 200
        resp.getheader.return_value = None
        resp.read.return_value = b""  # empty stream

        pipe_stream_response(h, resp)

        headers_sent = {c[0][0]: c[0][1] for c in h.send_header.call_args_list}
        assert headers_sent["Transfer-Encoding"] == "chunked"

    def test_records_ttft_on_first_chunk(self):
        """pipe_stream 第一个 chunk 后记录 TTFT。"""
        from inferfabric.forwarder import pipe_stream_response
        h = _make_handler()
        h._req_start = time.monotonic()

        resp = MagicMock()
        resp.status = 200
        resp.getheader.return_value = None
        resp.read.side_effect = [b"data: hello\n\n", b""]

        pipe_stream_response(h, resp)
        assert hasattr(h, '_ttft_ms')
        assert h._ttft_ms is not None

    def test_handles_broken_pipe_during_stream(self):
        """流传输中客户端断开不抛异常。"""
        from inferfabric.forwarder import pipe_stream_response
        h = _make_handler()
        resp = MagicMock()
        resp.status = 200
        resp.getheader.return_value = None
        resp.read.return_value = b"data: chunk\n\n"
        h.wfile.write.side_effect = BrokenPipeError()

        # Should not raise
        pipe_stream_response(h, resp)

    def test_ttft_defers_to_first_token_callback(self):
        """v6.1: sse_buf 挂首 token 回调 → ttft 由回调在首个内容 delta 时记录
        （首 chunk 兜底让位，不在 role 前言 chunk 上提前记）。"""
        from inferfabric.forwarder import pipe_stream_response
        from inferfabric.proxy.sse_buffer import SSELineBuffer, first_token_ttft_cb
        h = _make_handler()
        h._req_start = time.monotonic()
        h._ttft_ms = None

        role = b'data: {"choices": [{"delta": {"role": "assistant"}}]}\n\n'
        content = b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
        resp = MagicMock()
        resp.status = 200
        resp.getheader.return_value = None
        resp.read.side_effect = [role, content, b""]

        sse_buf = SSELineBuffer(first_token_ttft_cb(h))
        pipe_stream_response(h, resp, sse_buf)
        assert sse_buf.first_token_seen
        assert h._ttft_ms is not None  # 回调在 content chunk 解析时设置

    def test_ttft_none_when_stream_has_no_content(self):
        """v6.1: 流只有 role/usage 前言（无内容 delta）→ _ttft_ms 保持 None。"""
        from inferfabric.forwarder import pipe_stream_response
        from inferfabric.proxy.sse_buffer import SSELineBuffer, first_token_ttft_cb
        h = _make_handler()
        h._req_start = time.monotonic()
        h._ttft_ms = None

        role = b'data: {"choices": [{"delta": {"role": "assistant"}}]}\n\n'
        usage = (b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], '
                 b'"usage": {"prompt_tokens": 1, "completion_tokens": 1}}\n\n')
        resp = MagicMock()
        resp.status = 200
        resp.getheader.return_value = None
        resp.read.side_effect = [role, usage, b""]

        sse_buf = SSELineBuffer(first_token_ttft_cb(h))
        pipe_stream_response(h, resp, sse_buf)
        assert not sse_buf.first_token_seen
        assert h._ttft_ms is None


# ═══════════════════════════════════════════════════════════════
# 7. forward_anthropic_local
# ═══════════════════════════════════════════════════════════════


class TestForwardAnthropicLocal:

    @patch("inferfabric.forwarder.send_json")
    @patch("inferfabric.forwarder.HTTPConnection")
    def test_connection_refused_returns_503(self, mock_conn_class, mock_send):
        """连接被拒绝 → 重试耗尽后返回 503+None。"""
        from inferfabric.forwarder import forward_anthropic_local
        h = _make_handler()
        pm = MagicMock()
        pm.response_cache = None

        model = MagicMock()
        model.name = "test-vllm"
        model.port = 8000
        model.served_name = "test-model"

        mock_conn_class.return_value.request.side_effect = ConnectionRefusedError()

        result = forward_anthropic_local(h, pm,
            {"messages": [{"role": "user", "content": "hi"}]},
            "auth", model, "test-model")
        assert result is None
        mock_send.assert_called_once()

    @patch("inferfabric.forwarder.exponential_backoff")
    @patch("inferfabric.forwarder.send_json")
    @patch("inferfabric.forwarder.HTTPConnection")
    def test_retries_trigger_exponential_backoff(self, mock_conn, mock_send, mock_backoff):
        """连接失败触发 exponential_backoff 重试。"""
        from inferfabric.forwarder import forward_anthropic_local
        h = _make_handler()
        pm = MagicMock()
        pm.response_cache = None

        model = MagicMock()
        model.name = "test"
        model.port = 8000
        model.served_name = "test"

        mock_conn.return_value.request.side_effect = ConnectionRefusedError()
        mock_backoff.return_value = 0.01

        forward_anthropic_local(h, pm,
            {"messages": [{"role": "user", "content": "hi"}]},
            "auth", model, "test")
        assert mock_backoff.call_count >= 1