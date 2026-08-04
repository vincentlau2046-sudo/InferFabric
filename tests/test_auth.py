"""Tests for inferfabric.proxy.auth — AuthManager"""

import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest
import yaml

from inferfabric.proxy.auth import AuthManager, _KeyEntry


def _write_yaml(path: Path, data: dict):
    with open(path, "w") as f:
        yaml.dump(data, f)


class TestAuthDisabled:
    """api_keys.yaml 不存在或为空 → 鉴权关闭"""

    def test_no_config_file(self, tmp_path):
        mgr = AuthManager(tmp_path / "nonexistent.yaml")
        assert not mgr.enabled

    def test_empty_config(self, tmp_path):
        p = tmp_path / "api_keys.yaml"
        p.write_text("")
        mgr = AuthManager(p)
        assert not mgr.enabled

    def test_config_with_no_primary(self, tmp_path):
        p = tmp_path / "api_keys.yaml"
        _write_yaml(p, {"guests": [{"key": "g1", "name": "g"}]})
        mgr = AuthManager(p)
        assert not mgr.enabled


class TestAuthPrimary:
    """Primary key 校验"""

    def _mgr(self, tmp_path, primary="sk-test-primary", **extra):
        cfg = {"primary": primary, **extra}
        p = tmp_path / "api_keys.yaml"
        _write_yaml(p, cfg)
        return AuthManager(p)

    def test_enabled(self, tmp_path):
        mgr = self._mgr(tmp_path)
        assert mgr.enabled

    def test_valid_primary(self, tmp_path):
        mgr = self._mgr(tmp_path)
        ok, reason = mgr.check("sk-test-primary", "qwen36-35b-vl")
        assert ok and reason == "ok"

    def test_valid_primary_with_bearer_prefix(self, tmp_path):
        mgr = self._mgr(tmp_path)
        ok, reason = mgr.check("Bearer sk-test-primary", "qwen36-35b-vl")
        assert ok and reason == "ok"

    def test_invalid_key(self, tmp_path):
        mgr = self._mgr(tmp_path)
        ok, reason = mgr.check("sk-wrong", "qwen36-35b-vl")
        assert not ok and "invalid" in reason

    def test_empty_token(self, tmp_path):
        mgr = self._mgr(tmp_path)
        ok, reason = mgr.check("", "qwen36-35b-vl")
        assert not ok

    def test_primary_allows_all_models(self, tmp_path):
        mgr = self._mgr(tmp_path)
        for model in ["qwen36-35b-vl", "qwen35-9b-vl", "gemma4-31b-vl", "deepseek-v4-flash"]:
            ok, _ = mgr.check("sk-test-primary", model)
            assert ok, f"primary should allow model {model}"


class TestAuthGuest:
    """Guest key — 模型白名单 + 过期"""

    def _mgr(self, tmp_path, guests=None):
        cfg = {
            "primary": "sk-primary",
            "guests": guests or [],
        }
        p = tmp_path / "api_keys.yaml"
        _write_yaml(p, cfg)
        return AuthManager(p)

    def test_guest_allowed_model(self, tmp_path):
        mgr = self._mgr(tmp_path, guests=[
            {"key": "sk-guest1", "name": "test", "models": ["qwen35-9b-vl"]},
        ])
        ok, _ = mgr.check("sk-guest1", "qwen35-9b-vl")
        assert ok

    def test_guest_blocked_model(self, tmp_path):
        mgr = self._mgr(tmp_path, guests=[
            {"key": "sk-guest1", "name": "test", "models": ["qwen35-9b-vl"]},
        ])
        ok, reason = mgr.check("sk-guest1", "qwen36-35b-vl")
        assert not ok and "not allowed" in reason

    def test_guest_no_expiry(self, tmp_path):
        mgr = self._mgr(tmp_path, guests=[
            {"key": "sk-guest1", "name": "test", "models": None},
        ])
        ok, _ = mgr.check("sk-guest1", "any-model")
        assert ok

    def test_guest_not_expired(self, tmp_path):
        future = (datetime.now(tz=timezone.utc) + timedelta(days=30)).isoformat()
        mgr = self._mgr(tmp_path, guests=[
            {"key": "sk-guest1", "name": "test", "models": None, "expires": future},
        ])
        ok, _ = mgr.check("sk-guest1", "any-model")
        assert ok

    def test_guest_expired(self, tmp_path):
        past = (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()
        mgr = self._mgr(tmp_path, guests=[
            {"key": "sk-guest1", "name": "test", "models": None, "expires": past},
        ])
        ok, reason = mgr.check("sk-guest1", "any-model")
        assert not ok and "expired" in reason


class TestAuthKeyName:
    """key_name() 用于日志记录"""

    def test_primary_name(self, tmp_path):
        p = tmp_path / "api_keys.yaml"
        _write_yaml(p, {"primary": "sk-p"})
        mgr = AuthManager(p)
        assert mgr.key_name("sk-p") == "primary"
        assert mgr.key_name("Bearer sk-p") == "primary"

    def test_guest_name(self, tmp_path):
        p = tmp_path / "api_keys.yaml"
        _write_yaml(p, {"primary": "sk-p", "guests": [
            {"key": "sk-g", "name": "测试用"},
        ]})
        mgr = AuthManager(p)
        assert mgr.key_name("sk-g") == "测试用"

    def test_unknown_key(self, tmp_path):
        p = tmp_path / "api_keys.yaml"
        _write_yaml(p, {"primary": "sk-p"})
        mgr = AuthManager(p)
        assert mgr.key_name("sk-unknown") == "anonymous"


class TestAuthReload:
    """热加载"""

    def test_reload_enables_auth(self, tmp_path):
        p = tmp_path / "api_keys.yaml"
        p.write_text("")  # initially empty
        mgr = AuthManager(p)
        assert not mgr.enabled

        _write_yaml(p, {"primary": "sk-new"})
        mgr.reload(p)
        assert mgr.enabled
        ok, _ = mgr.check("sk-new", "any")
        assert ok

    def test_reload_disables_auth(self, tmp_path):
        p = tmp_path / "api_keys.yaml"
        _write_yaml(p, {"primary": "sk-old"})
        mgr = AuthManager(p)
        assert mgr.enabled

        p.write_text("")  # clear
        mgr.reload(p)
        assert not mgr.enabled
