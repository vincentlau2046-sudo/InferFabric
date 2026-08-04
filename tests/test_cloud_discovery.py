"""Tests for inferfabric.cloud_discovery — CloudDiscovery"""

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
import time

import pytest
import yaml

from inferfabric.cloud_discovery import CloudDiscovery, CloudModel, ProviderConfig


def _write_yaml(path: Path, data: dict):
    with open(path, "w") as f:
        yaml.dump(data, f)


# ── Mock HTTP server for /models ──

class _MockModelsHandler(BaseHTTPRequestHandler):
    """Returns a canned /models response."""
    MODELS_RESPONSE = {
        "data": [
            {"id": "deepseek-v4-flash", "object": "model"},
            {"id": "glm-5", "object": "model"},
            {"id": "qwen3.5-72b", "object": "model"},
            {"id": "internal-test-model", "object": "model"},
        ]
    }

    def do_GET(self):
        if self.path == "/models":
            body = json.dumps(self.MODELS_RESPONSE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress logs


@pytest.fixture(scope="module")
def mock_server():
    """Start a mock /models HTTP server on a free port."""
    server = HTTPServer(("127.0.0.1", 0), _MockModelsHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


class TestProviderConfig:
    """ProviderConfig dataclass defaults"""

    def test_defaults(self):
        cfg = ProviderConfig(name="test")
        assert cfg.enabled
        assert cfg.discovery_enabled
        assert cfg.discovery_interval == 3600
        assert cfg.routing_default == "cloud_only"


class TestCloudDiscoveryConfig:
    """配置加载"""

    def test_no_config(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "nonexistent.yaml")
        assert len(cd.providers) == 0

    def test_empty_config(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        p.write_text("")
        cd = CloudDiscovery(p)
        assert len(cd.providers) == 0

    def test_load_single_provider(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "baidu-codingplan": {
                    "api_key": "sk-test",
                    "openai_base": "https://example.com/v1",
                    "anthropic_base": "https://example.com/anthropic/v1",
                    "timeout": 30,
                    "enabled": True,
                    "discovery": {
                        "enabled": True,
                        "endpoint": "/models",
                        "interval": 1800,
                        "filter": {"include_pattern": "^(deepseek|glm).*"},
                    },
                    "routing": {"default": "cloud_only"},
                }
            }
        })
        cd = CloudDiscovery(p)
        assert "baidu-codingplan" in cd.providers
        cfg = cd.providers["baidu-codingplan"]
        assert cfg.api_key == "sk-test"
        assert cfg.include_pattern == "^(deepseek|glm).*"
        assert cfg.discovery_interval == 1800

    def test_load_disabled_provider(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "disabled-one": {"enabled": False, "openai_base": "https://x.com"},
            }
        })
        cd = CloudDiscovery(p)
        assert "disabled-one" in cd.providers
        assert not cd.providers["disabled-one"].enabled


class TestCloudDiscoveryDiscover:
    """模型发现"""

    def test_discover_with_filter(self, tmp_path, mock_server):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "test-provider": {
                    "api_key": "sk-test",
                    "openai_base": f"http://127.0.0.1:{mock_server}",
                    "anthropic_base": f"http://127.0.0.1:{mock_server}/anthropic",
                    "discovery": {
                        "filter": {"include_pattern": "^(deepseek|glm).*"},
                    },
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        # Should include deepseek-v4-flash and glm-5, but not qwen3.5-72b or internal-test-model
        assert "deepseek-v4-flash" in models
        assert "glm-5" in models
        assert "qwen3.5-72b" not in models
        assert "internal-test-model" not in models

    def test_discover_no_filter(self, tmp_path, mock_server):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "test-provider": {
                    "api_key": "sk-test",
                    "openai_base": f"http://127.0.0.1:{mock_server}",
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        # 4 short-name keys + 4 provider-prefixed keys = 8
        assert len(models) == 8
        # All 4 unique model IDs exist
        model_ids = {m.model_id for m in models.values()}
        assert model_ids == {"deepseek-v4-flash", "glm-5", "qwen3.5-72b", "internal-test-model"}

    def test_discover_anthropic_flag(self, tmp_path, mock_server):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "test-provider": {
                    "api_key": "sk-test",
                    "openai_base": f"http://127.0.0.1:{mock_server}",
                    "anthropic_base": f"http://127.0.0.1:{mock_server}/anthropic",
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        for m in models.values():
            assert m.anthropic_available is True

    def test_discover_no_anthropic_base(self, tmp_path, mock_server):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "test-provider": {
                    "api_key": "sk-test",
                    "openai_base": f"http://127.0.0.1:{mock_server}",
                    # no anthropic_base
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        for m in models.values():
            assert m.anthropic_available is False

    def test_discover_provider_down(self, tmp_path):
        """Provider 不可达时返回空列表，不抛异常。"""
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "dead-provider": {
                    "api_key": "sk-test",
                    "openai_base": "http://127.0.0.1:1",  # unreachable
                    "timeout": 1,
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        assert len(models) == 0

    def test_discover_disabled_provider_skipped(self, tmp_path, mock_server):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "off": {
                    "enabled": False,
                    "openai_base": f"http://127.0.0.1:{mock_server}",
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        assert len(models) == 0


class TestCloudDiscoveryRoute:
    """路由决策"""

    def test_route_local(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "none.yaml")
        cd._cloud_models = {
            "deepseek-v4-flash": CloudModel("deepseek-v4-flash", "baidu-codingplan"),
        }
        result = cd.resolve_route("qwen36-35b-vl", local_models={"qwen36-35b-vl"})
        assert result == "local"

    def test_route_cloud(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "none.yaml")
        cd._cloud_models = {
            "deepseek-v4-flash": CloudModel("deepseek-v4-flash", "baidu-codingplan"),
        }
        result = cd.resolve_route("deepseek-v4-flash", local_models=set())
        assert result == "cloud:baidu-codingplan"

    def test_route_with_provider_prefix(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "none.yaml")
        cd._cloud_models = {
            "glm-5": CloudModel("glm-5", "baidu-codingplan"),
        }
        result = cd.resolve_route("baidu-codingplan/glm-5", local_models=set())
        assert result == "cloud:baidu-codingplan"

    def test_route_no_match(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "none.yaml")
        cd._cloud_models = {}
        result = cd.resolve_route("unknown-model", local_models=set())
        assert result is None

    def test_route_local_takes_priority(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "none.yaml")
        cd._cloud_models = {
            "qwen36-35b-vl": CloudModel("qwen36-35b-vl", "baidu-codingplan"),
        }
        result = cd.resolve_route("qwen36-35b-vl", local_models={"qwen36-35b-vl"})
        assert result == "local"
