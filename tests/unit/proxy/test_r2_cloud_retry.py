"""
unit/proxy/test_r2_cloud_retry.py — R2: 云端转发退避重试

测试对象: inferfabric.forwarder.forward_to_cloud
覆盖范围:
  - HTTP 429 → 重试后成功
  - HTTP 500 → 重试后成功
  - 非重试 4xx (400) → 不重试直接返回
  - 3 次 429 全部失败 → 最终返回 error
  - ConnectionError / Timeout → 重试
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError as _UrllibHTTPError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
from inferfabric.cloud_discovery import ProviderConfig, CloudModel
from inferfabric.forwarder import forward_to_cloud, CloudResult


class _WFile:
    def __init__(self):
        self.data = b""
    def write(self, b):
        self.data += b
    def flush(self):
        pass


class _FakeHandler:
    def __init__(self):
        self.wfile = _WFile()
        self.status = None
        self.sent_headers = {}
        self.headers = {}
    def send_response(self, status):
        self.status = status
    def send_header(self, k, v):
        self.sent_headers[k] = str(v)
    def end_headers(self):
        pass


def _make_cfg(**kw):
    return ProviderConfig(
        name="test-provider",
        api_key="sk-test",
        openai_base="https://example.com/v1",
        **kw,
    )


def _make_cm():
    return CloudModel(
        model_id="haiku",
        provider="test-provider",
        openai_available=True,
        anthropic_available=True,
    )


class _FakeResp:
    """Mock urllib.response.addinfourl 替身。"""
    def __init__(self, status, body=b'{"ok":true,"usage":{}}'):
        self.status = status
        self._body = body
    def read(self):
        return self._body
    def close(self):
        pass
    def getheader(self, h, default=None):
        return default


class _FakeHTTPError(_UrllibHTTPError):
    """模拟 urllib.error.HTTPError。"""
    def __init__(self, code, body=b'{"error":"test"}'):
        from io import BytesIO
        super().__init__("http://fake/", code, f"HTTP {code}", {}, BytesIO(body))
        self._body = body
    def read(self):
        return self._body
    def close(self):
        pass


# ═══════════════════════════════════════════════════════════════
# 1. 可重试的 HTTP 错误（429 / 5xx）
# ═══════════════════════════════════════════════════════════════


@patch("inferfabric.forwarder.urlopen")
def test_retry_429_then_success(mock_urlopen):
    """429 → 重试 1 次 → 返回 200。"""
    mock_urlopen.side_effect = [
        _FakeHTTPError(429),  # 第一次 429
        _FakeResp(200),         # 第二次 200
    ]
    h = _FakeHandler()
    data = {"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}
    result = forward_to_cloud(h, data, _make_cfg(), _make_cm(), protocol="openai")
    assert result.status == 200
    assert mock_urlopen.call_count == 2


@patch("inferfabric.forwarder.urlopen")
def test_retry_500_then_success(mock_urlopen):
    """500 → 重试 1 次 → 返回 200。"""
    mock_urlopen.side_effect = [
        _FakeHTTPError(500),
        _FakeResp(200),
    ]
    h = _FakeHandler()
    data = {"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}
    result = forward_to_cloud(h, data, _make_cfg(), _make_cm(), protocol="openai")
    assert result.status == 200
    assert mock_urlopen.call_count == 2


@patch("inferfabric.forwarder.urlopen")
def test_retry_503_then_success(mock_urlopen):
    """503 → 重试 → 200。"""
    mock_urlopen.side_effect = [
        _FakeHTTPError(503),
        _FakeResp(200),
    ]
    h = _FakeHandler()
    data = {"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}
    result = forward_to_cloud(h, data, _make_cfg(), _make_cm(), protocol="openai")
    assert result.status == 200
    assert mock_urlopen.call_count == 2


# ═══════════════════════════════════════════════════════════════
# 2. 不可重试的错误
# ═══════════════════════════════════════════════════════════════


@patch("inferfabric.forwarder.urlopen")
def test_no_retry_on_400(mock_urlopen):
    """400（不可重试）→ 不重试，直接返回 error。"""
    mock_urlopen.side_effect = _FakeHTTPError(400)
    h = _FakeHandler()
    data = {"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}
    result = forward_to_cloud(h, data, _make_cfg(), _make_cm(), protocol="openai")
    assert result.status == 400  # 4xx 透传（不可重试）
    assert mock_urlopen.call_count == 1  # 仅 1 次尝试


@patch("inferfabric.forwarder.urlopen")
def test_give_up_after_3_retries(mock_urlopen):
    """3 次全是 429 → 最终返回 error（不再重试）。"""
    mock_urlopen.side_effect = [
        _FakeHTTPError(429),
        _FakeHTTPError(429),
        _FakeHTTPError(429),
    ]
    h = _FakeHandler()
    data = {"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}
    result = forward_to_cloud(h, data, _make_cfg(), _make_cm(), protocol="openai")
    assert result.status == 429  # 最后一次的 HTTP 状态透传
    assert mock_urlopen.call_count == 3


# ═══════════════════════════════════════════════════════════════
# 3. 连接错误重试
# ═══════════════════════════════════════════════════════════════


@patch("inferfabric.forwarder.urlopen")
def test_retry_on_timeout(mock_urlopen):
    """TimeoutError → 重试 1 次 → 200。"""
    mock_urlopen.side_effect = [
        TimeoutError("timed out"),
        _FakeResp(200),
    ]
    h = _FakeHandler()
    data = {"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}
    result = forward_to_cloud(h, data, _make_cfg(), _make_cm(), protocol="openai")
    assert result.status == 200
    assert mock_urlopen.call_count == 2


@patch("inferfabric.forwarder.urlopen")
def test_give_up_after_3_timeouts(mock_urlopen):
    '''TimeoutError 3 次 -> 最终 503。'''
    mock_urlopen.side_effect = [
        TimeoutError("t1"),
        TimeoutError("t2"),
        TimeoutError("t3"),
    ]
    h = _FakeHandler()
    data = {"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}
    result = forward_to_cloud(h, data, _make_cfg(), _make_cm(), protocol="openai")
    assert result.status == 503  # 所有重试耗尽
    assert mock_urlopen.call_count == 3


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))