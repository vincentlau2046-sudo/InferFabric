"""tests/test_gap_phase1_ssrf.py — D-3: SSRF protection"""

import socket
from unittest.mock import MagicMock, patch

import pytest

from inferfabric.proxy.handler import ProxyHandler


class FakeServer:
    """Minimal fake server for ProxyHandler."""
    def __init__(self, proxy_mgr=None):
        self.proxy_mgr = proxy_mgr


def _make_dns_mock(ip_map):
    """Return a mock for socket.getaddrinfo.

    ip_map: {hostname: [ip_str, ...]}.
    Unmapped hostnames resolve to public IP 1.2.3.4.
    """
    def _getaddrinfo(host, *args, **kwargs):
        ips = ip_map.get(host, ["1.2.3.4"])
        results = []
        for ip in ips:
            family = 10 if ":" in ip else 2
            results.append((family, 1, 0, "", (ip, 0)))
        return results
    return _getaddrinfo


def _handler_with_cloud(provider_hosts):
    """Create a ProxyHandler with mocked cloud registry."""
    handler = ProxyHandler.__new__(ProxyHandler)

    mock_cloud = MagicMock()
    mock_providers = {}
    for host in provider_hosts:
        mock_cfg = MagicMock()
        mock_cfg.openai_base = f"https://{host}/v1"
        mock_cfg.anthropic_base = ""
        mock_providers[host.replace(".", "_")] = mock_cfg
    mock_cloud.providers = mock_providers

    mock_pm = MagicMock()
    mock_pm.cloud = mock_cloud

    handler.server = FakeServer(proxy_mgr=mock_pm)
    return handler


class TestSSRFValidateUrl:
    """Test _validate_cloud_test_url against various attack vectors."""

    def test_private_ip_127(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=_make_dns_mock({"127.0.0.1": ["127.0.0.1"]})):
            valid, reason, _ = h._validate_cloud_test_url("https://127.0.0.1/models", pm)
        assert not valid
        assert "private" in reason.lower()

    def test_private_ip_10(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=_make_dns_mock({"10.0.0.1": ["10.0.0.1"]})):
            valid, reason, _ = h._validate_cloud_test_url("https://10.0.0.1/models", pm)
        assert not valid

    def test_private_ip_172_16(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=_make_dns_mock({"172.16.0.1": ["172.16.0.1"]})):
            valid, reason, _ = h._validate_cloud_test_url("https://172.16.0.1/models", pm)
        assert not valid

    def test_private_ip_192_168(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=_make_dns_mock({"192.168.1.1": ["192.168.1.1"]})):
            valid, reason, _ = h._validate_cloud_test_url("https://192.168.1.1/models", pm)
        assert not valid

    def test_link_local_169_254(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=_make_dns_mock({"169.254.1.1": ["169.254.1.1"]})):
            valid, reason, _ = h._validate_cloud_test_url("https://169.254.1.1/models", pm)
        assert not valid

    def test_ipv6_loopback(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=_make_dns_mock({"::1": ["::1"]})):
            valid, reason, _ = h._validate_cloud_test_url("https://[::1]/models", pm)
        assert not valid

    def test_ipv6_private(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=_make_dns_mock({"fc00::1": ["fc00::1"]})):
            valid, reason, _ = h._validate_cloud_test_url("https://[fc00::1]/models", pm)
        assert not valid

    def test_non_https_scheme(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        # No DNS needed — scheme check happens first
        valid, reason, _ = h._validate_cloud_test_url("http://api.openai.com/models", pm)
        assert not valid
        assert "https" in reason.lower()

    def test_ftp_scheme(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        valid, reason, _ = h._validate_cloud_test_url("ftp://api.openai.com/models", pm)
        assert not valid

    def test_non_whitelisted_host(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=_make_dns_mock({})):  # evil.com → 1.2.3.4 (public)
            valid, reason, _ = h._validate_cloud_test_url("https://evil.com/models", pm)
        assert not valid
        assert "not a registered" in reason.lower() or "registry" in reason.lower()

    def test_valid_url(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=_make_dns_mock({"api.openai.com": ["1.2.3.4"]})):
            valid, reason, _ = h._validate_cloud_test_url("https://api.openai.com/models", pm)
        assert valid, f"Valid cloud URL should pass: {reason}"

    def test_valid_url_with_path(self):
        h = _handler_with_cloud(["api.openai.com", "api.anthropic.com"])
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=_make_dns_mock({"api.anthropic.com": ["1.2.3.4"]})):
            valid, reason, _ = h._validate_cloud_test_url("https://api.anthropic.com/v1/models", pm)
        assert valid, f"Valid Anthropic URL should pass: {reason}"

    def test_invalid_url_malformed(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        valid, reason, _ = h._validate_cloud_test_url("not-a-url", pm)
        assert not valid

    def test_empty_url(self):
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        valid, reason, _ = h._validate_cloud_test_url("", pm)
        assert not valid

    def test_dns_rebinding_private_ip(self):
        """Whitelisted host resolves to private IP → rejected (DNS rebinding)."""
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=_make_dns_mock({"api.openai.com": ["10.0.0.1"]})):
            valid, reason, _ = h._validate_cloud_test_url("https://api.openai.com/models", pm)
        assert not valid
        assert "private" in reason.lower()

    def test_dns_resolution_failure(self):
        """DNS failure → rejected."""
        h = _handler_with_cloud(["api.openai.com"])
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=socket.gaierror("DNS failed")):
            valid, reason, _ = h._validate_cloud_test_url("https://api.openai.com/models", pm)
        assert not valid


class TestHandleCloudTestSSRF:
    """Integration test for _handle_cloud_test SSRF gate."""

    def test_blocked_by_ssrf(self):
        h = _handler_with_cloud(["api.openai.com"])
        h._read_body = MagicMock(return_value={"url": "https://127.0.0.1/admin", "api_key": "test"})
        h._send_json = MagicMock()
        pm = h.server.proxy_mgr
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                    side_effect=_make_dns_mock({"127.0.0.1": ["127.0.0.1"]})):
            h._handle_cloud_test(pm)
        h._send_json.assert_called_once()
        args, kwargs = h._send_json.call_args
        assert args[1] == 400
        assert "SSRF" in str(args[0])
