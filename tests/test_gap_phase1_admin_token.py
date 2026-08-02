"""tests/test_gap_phase1_admin_token.py — D-4: admin token security"""

import os
import hmac
import importlib
from unittest.mock import MagicMock, patch

import pytest


class TestCheckAdminConstantTime:
    """Verify _check_admin uses hmac.compare_digest."""

    def test_compare_digest_usage(self):
        """Source code must use hmac.compare_digest, not ==."""
        from inferfabric.proxy.handler import ProxyHandler
        import inspect
        source = inspect.getsource(ProxyHandler._check_admin)
        assert "hmac.compare_digest" in source, \
            "_check_admin must use hmac.compare_digest for constant-time comparison"
        # Should NOT use bare == for token comparison
        stripped = source.replace(" ", "")
        assert 'token==_ADMIN_TOKEN' not in stripped and \
               'token=="_ADMIN_TOKEN"' not in source, \
            "_check_admin should not use == for token comparison"


class TestValidateAdminTokenSafety:
    """Verify _validate_admin_token_safety enforces fail-fast.

    NOTE: PROXY_HOST and _ADMIN_TOKEN are module-level constants in handler.py,
    read from os.environ at import time. monkeypatch.setenv alone won't work
    because the module is already cached in sys.modules. We must use
    monkeypatch.setattr on the module attributes directly.
    """

    def test_empty_token_non_localhost_raises(self, monkeypatch):
        """Empty token + non-localhost PROXY_HOST → RuntimeError."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "PROXY_HOST", "0.0.0.0")
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "")
        with pytest.raises(RuntimeError) as excinfo:
            h_mod._validate_admin_token_safety()
        assert "not localhost" in str(excinfo.value).lower() or \
               "IFF_ADMIN_TOKEN" in str(excinfo.value)

    def test_empty_token_localhost_ok(self, monkeypatch):
        """Empty token + localhost PROXY_HOST → no error, just warning."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "PROXY_HOST", "127.0.0.1")
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "")
        h_mod._validate_admin_token_safety()  # should not raise

    def test_empty_token_localhost_ipv6_ok(self, monkeypatch):
        """Empty token + ::1 PROXY_HOST → no error."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "PROXY_HOST", "::1")
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "")
        h_mod._validate_admin_token_safety()

    def test_empty_token_localhost_name_ok(self, monkeypatch):
        """Empty token + 'localhost' PROXY_HOST → no error."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "PROXY_HOST", "localhost")
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "")
        h_mod._validate_admin_token_safety()

    def test_token_set_any_host_ok(self, monkeypatch):
        """Token set + any PROXY_HOST → no error."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "PROXY_HOST", "0.0.0.0")
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "secret123")
        h_mod._validate_admin_token_safety()


class TestAdminTokenIntegration:
    """Integration tests for _check_admin in ProxyHandler."""

    def test_correct_token_accepted(self, monkeypatch):
        """Correct token passes check."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "test-secret")
        h = h_mod.ProxyHandler.__new__(h_mod.ProxyHandler)
        h.headers = MagicMock()
        h.headers.get.return_value = "test-secret"
        h._send_json = MagicMock()

        result = h._check_admin()
        assert result is True
        h._send_json.assert_not_called()

    def test_wrong_token_rejected(self, monkeypatch):
        """Wrong token fails check with 401."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "test-secret")
        h = h_mod.ProxyHandler.__new__(h_mod.ProxyHandler)
        h.headers = MagicMock()
        h.headers.get.return_value = "wrong-secret"
        h._send_json = MagicMock()

        result = h._check_admin()
        assert result is False
        h._send_json.assert_called_once()
        args, _ = h._send_json.call_args
        assert args[1] == 401

    def test_empty_token_config_allows_all(self, monkeypatch):
        """When _ADMIN_TOKEN is empty, all requests pass."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "")
        h = h_mod.ProxyHandler.__new__(h_mod.ProxyHandler)
        h.headers = MagicMock()
        h._send_json = MagicMock()

        result = h._check_admin()
        assert result is True
        h._send_json.assert_not_called()

    def test_empty_header_rejected(self, monkeypatch):
        """Empty X-Admin-Token header is rejected when token is set."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "secret")
        h = h_mod.ProxyHandler.__new__(h_mod.ProxyHandler)
        h.headers = MagicMock()
        h.headers.get.return_value = ""
        h._send_json = MagicMock()

        result = h._check_admin()
        assert result is False
