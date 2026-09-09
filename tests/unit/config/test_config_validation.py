"""
unit/config/test_config_validation.py — iff.yaml 配置校验测试

测试对象: inferfabric.proxy_manager.ProxyManager._validate_runtime_config
覆盖范围:
  - rate_limit.mode（reject / observe / 非法值）
  - rate_limit.timeout / server_rpm / model_rpm_default（类型、范围）
  - rate_limit.max_concurrent（正整数、auto、负数、布尔）
  - access_log_jsonl / request_log_retention_days 类型校验
  - _load_runtime_config 加载 & 优雅降级
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import inferfabric.proxy  # noqa: F401 — resolve circular import
from inferfabric.proxy_manager import ProxyManager
from inferfabric.config import ConfigError


@pytest.fixture
def pm():
    """Create a bare ProxyManager without side effects."""
    return ProxyManager.__new__(ProxyManager)


class TestValidateRuntimeConfig:
    """Test _validate_runtime_config against valid and invalid configs."""

    def test_valid_minimal_config(self, pm):
        """Empty config passes validation (all defaults are valid)."""
        pm._validate_runtime_config({})

    def test_valid_full_config(self, pm):
        """Complete valid config passes validation."""
        config = {
            "rate_limit": {
                "mode": "reject",
                "timeout": 10,
                "server_rpm": 100,
                "model_rpm_default": 50,
                "max_concurrent": 20,
            },
            "access_log_jsonl": True,
            "request_log_retention_days": 30,
        }
        pm._validate_runtime_config(config)

    def test_valid_observe_mode(self, pm):
        pm._validate_runtime_config({"rate_limit": {"mode": "observe"}})

    def test_valid_auto_max_concurrent(self, pm):
        pm._validate_runtime_config({"rate_limit": {"max_concurrent": "auto"}})

    def test_invalid_mode(self, pm):
        with pytest.raises(ConfigError, match="mode"):
            pm._validate_runtime_config({"rate_limit": {"mode": "block"}})

    def test_invalid_timeout_zero(self, pm):
        with pytest.raises(ConfigError, match="timeout"):
            pm._validate_runtime_config({"rate_limit": {"timeout": 0}})

    def test_invalid_timeout_negative(self, pm):
        with pytest.raises(ConfigError, match="timeout"):
            pm._validate_runtime_config({"rate_limit": {"timeout": -1}})

    def test_invalid_timeout_string(self, pm):
        with pytest.raises(ConfigError, match="timeout"):
            pm._validate_runtime_config({"rate_limit": {"timeout": "5s"}})

    def test_invalid_server_rpm_negative(self, pm):
        with pytest.raises(ConfigError, match="server_rpm"):
            pm._validate_runtime_config({"rate_limit": {"server_rpm": -1}})

    def test_invalid_server_rpm_float(self, pm):
        with pytest.raises(ConfigError, match="server_rpm"):
            pm._validate_runtime_config({"rate_limit": {"server_rpm": 3.5}})

    def test_invalid_model_rpm_default_negative(self, pm):
        with pytest.raises(ConfigError, match="model_rpm_default"):
            pm._validate_runtime_config({"rate_limit": {"model_rpm_default": -1}})

    def test_invalid_max_concurrent_zero(self, pm):
        with pytest.raises(ConfigError, match="max_concurrent"):
            pm._validate_runtime_config({"rate_limit": {"max_concurrent": 0}})

    def test_invalid_max_concurrent_negative(self, pm):
        with pytest.raises(ConfigError, match="max_concurrent"):
            pm._validate_runtime_config({"rate_limit": {"max_concurrent": -5}})

    def test_invalid_max_concurrent_bad_string(self, pm):
        with pytest.raises(ConfigError, match="max_concurrent"):
            pm._validate_runtime_config({"rate_limit": {"max_concurrent": "disable"}})

    def test_invalid_max_concurrent_bool(self, pm):
        with pytest.raises(ConfigError, match="max_concurrent"):
            pm._validate_runtime_config({"rate_limit": {"max_concurrent": True}})

    def test_invalid_access_log_jsonl(self, pm):
        with pytest.raises(ConfigError, match="access_log_jsonl"):
            pm._validate_runtime_config({"access_log_jsonl": "yes"})

    def test_invalid_retention_zero(self, pm):
        with pytest.raises(ConfigError, match="request_log_retention_days"):
            pm._validate_runtime_config({"request_log_retention_days": 0})

    def test_invalid_retention_negative(self, pm):
        with pytest.raises(ConfigError, match="request_log_retention_days"):
            pm._validate_runtime_config({"request_log_retention_days": -30})

    def test_invalid_retention_string(self, pm):
        with pytest.raises(ConfigError, match="request_log_retention_days"):
            pm._validate_runtime_config({"request_log_retention_days": "90d"})

    def test_rate_limit_not_dict(self, pm):
        with pytest.raises(ConfigError, match="mapping"):
            pm._validate_runtime_config({"rate_limit": "observe"})

    def test_max_concurrent_int_negative(self, pm):
        with pytest.raises(ConfigError, match="max_concurrent"):
            pm._validate_runtime_config({"rate_limit": {"max_concurrent": -1}})


class TestLoadRuntimeConfigValidation:
    """Verify _load_runtime_config calls validation on valid YAML."""

    def test_valid_yaml_passes(self, pm, tmp_path, monkeypatch):
        """A valid iff.yaml should load without error."""
        monkeypatch.setattr(
            "inferfabric.proxy_manager.IFF_DATA_DIR",
            tmp_path,
        )
        config_path = tmp_path / "iff.yaml"
        config_path.write_text(yaml.dump({
            "rate_limit": {
                "mode": "reject",
                "timeout": 10,
                "server_rpm": 100,
                "model_rpm_default": 50,
                "max_concurrent": "auto",
            },
            "access_log_jsonl": True,
            "request_log_retention_days": 90,
        }))

        cfg = pm._load_runtime_config()
        assert cfg["rate_limit"]["mode"] == "reject"

    def test_invalid_yaml_returns_empty(self, pm, tmp_path, monkeypatch):
        """An invalid iff.yaml should return {} (graceful degradation)."""
        monkeypatch.setattr(
            "inferfabric.proxy_manager.IFF_DATA_DIR",
            tmp_path,
        )
        config_path = tmp_path / "iff.yaml"
        config_path.write_text(yaml.dump({
            "rate_limit": {"mode": "invalid_mode"},
        }))

        with patch("inferfabric.proxy_manager.log") as mock_log:
            cfg = pm._load_runtime_config()
            assert cfg == {}, "Invalid config should return empty dict"

    def test_no_config_file_returns_empty(self, pm, tmp_path, monkeypatch):
        """No iff.yaml → return {}."""
        monkeypatch.setattr(
            "inferfabric.proxy_manager.IFF_DATA_DIR",
            tmp_path / "nonexistent",
        )
        cfg = pm._load_runtime_config()
        assert cfg == {}
