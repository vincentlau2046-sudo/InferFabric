"""
unit/proxy/test_post_provider_key_expand.py — B1: POST provider 后内存 api_key 需展开

B1 (CRIT/HIGH, review §B1 / §8.8 / §7.7): POST /admin/cloud/providers 添加 provider
   后，内存里 ProviderConfig.api_key 存的是未展开的字面量 ${REF}（如 ${IFF_OPENAI_KEY}）。
   而转发层 (forwarder.forward_to_cloud) 与发现层 (CloudDiscovery._discover_provider)
   都直接把 provider_cfg.api_key 塞进 Authorization 头 → 直到 reload/重启前，实际发给
   云端的是字面量 ${VAR} → 401。dashboard 添加 provider 的主流程因此坏掉。

   修复：POST 时内存持有真实 key（与 _load_config 在解析前展开 ${VAR} 的行为一致）；
   持久化由 _serialize_providers 依 key_env_var 重新导出 ${REF}，YAML 仍只存引用、
   明文只落 secrets.env（0600）。

   TDD 锚点（review §7.7）：新增"POST provider 后立即转发"测试——POST 明文 key 后，
   内存 api_key 即真实值，forward_to_cloud 生成的 Authorization 头不再是字面量 ${VAR}。

隔离说明：用真实 CloudDiscovery（tmp config/secrets 路径）+ 遮蔽 _inject_secrets_env，
   避免触碰真实 ~/.inferfabric/secrets.env 与真实 HOME 环境变量。
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

from inferfabric.cloud_discovery import CloudDiscovery, SecretsManager
from inferfabric.proxy.handler import ProxyHandler


# ─── 桩构造 ──────────────────────────────────────────────────


def _make_handler(command: str, body: dict):
    """最小 ProxyHandler 替身：记录 _send_json 的 (data, status)，stub 掉 body 读取。"""
    h = ProxyHandler.__new__(ProxyHandler)
    h.command = command
    h.path = "/admin/cloud/providers"
    h.headers = {}
    h._read_body = lambda: body
    h._sent = []  # (data, status)

    def _send_json(data, status=200, extra_headers=None):
        h._sent.append((data, status))

    h._send_json = _send_json
    return h


def _make_pm(tmp_path: Path):
    """真实 CloudDiscovery（tmp config/secrets 路径）+ 遮蔽 _inject_secrets_env 以隔离
    真实 HOME。secrets 走 tmp 路径，save_config 写 tmp config。"""
    cfg_path = tmp_path / "cloud_provider.yaml"
    secrets_path = tmp_path / "secrets.env"

    cd = CloudDiscovery(config_path=None)   # 不触发 _load_config（避免读真实 secrets.env）
    cd._config_path = cfg_path              # save_config 落到 tmp
    cd._secrets_mgr = SecretsManager(path=secrets_path)
    cd._inject_secrets_env = lambda: None   # 遮蔽：确定化 + 不碰真实 HOME 环境

    pm = SimpleNamespace(cloud=cd)
    return pm, cd, cfg_path, secrets_path


def _last_sent(h) -> tuple:
    assert h._sent, "handler did not send a response"
    return h._sent[-1]


# ═══════════════════════════════════════════════════════════════
# B1 核心：POST 明文 key → 内存即真实值（立即转发不再 401）
# ═══════════════════════════════════════════════════════════════


class TestPostProviderKeyExpand:

    def test_post_plaintext_key_inmemory_is_real_not_ref(self, tmp_path):
        """review §7.7 TDD 锚点：POST 明文 key 后，内存 api_key = 真实值（非 ${REF}）。"""
        pm, cd, cfg_path, secrets_path = _make_pm(tmp_path)
        h = _make_handler("POST", {
            "name": "openai-x",
            "api_key": "sk-real-123",
            "openai_base": "https://api.x.com/v1",
        })
        h._handle_cloud_providers(pm)

        data, status = _last_sent(h)
        assert status == 200, f"POST should succeed, got {status}: {data}"

        cfg = pm.cloud.get_provider_config("openai-x")
        assert cfg is not None
        # B1: 内存持有真实 key（forward_to_cloud 会把它塞进 Authorization）
        assert cfg.api_key == "sk-real-123"
        assert not cfg.api_key.startswith("${"), "api_key must not be an unexpanded ${REF}"
        # key_env_var 仍指向持久化用 ENV 名
        assert cfg.key_env_var == "IFF_OPENAI_X_KEY"

    def test_post_plaintext_key_immediate_forward_header(self, tmp_path):
        """立即转发视角：forward_to_cloud 生成的 Authorization 头是真实 key，非字面量。"""
        pm, cd, cfg_path, secrets_path = _make_pm(tmp_path)
        h = _make_handler("POST", {
            "name": "openai-x",
            "api_key": "sk-real-123",
            "openai_base": "https://api.x.com/v1",
        })
        h._handle_cloud_providers(pm)

        cfg = pm.cloud.get_provider_config("openai-x")
        # forwarder.py:221 的用法：Authorization: Bearer {provider_cfg.api_key}
        auth = f"Bearer {cfg.api_key}"
        assert auth == "Bearer sk-real-123"
        assert "${" not in auth, "Authorization header must not leak the ${REF} literal"

    def test_post_ref_key_resolved_from_env(self, tmp_path, monkeypatch):
        """传入 ${REF}（而非明文）→ 注入 env 后按引用解析为真实值。"""
        monkeypatch.setenv("IFF_OPENAI_KEY", "sk-from-env")
        pm, cd, cfg_path, secrets_path = _make_pm(tmp_path)
        # name "openai" → 派生 env var IFF_OPENAI_KEY；传入引用 ${IFF_OPENAI_KEY}
        h = _make_handler("POST", {
            "name": "openai",
            "api_key": "${IFF_OPENAI_KEY}",
            "openai_base": "https://api.x.com/v1",
        })
        h._handle_cloud_providers(pm)

        cfg = pm.cloud.get_provider_config("openai")
        assert cfg is not None
        assert cfg.api_key == "sk-from-env", "a passed ${REF} must resolve to the env value"
        assert not cfg.api_key.startswith("${")


# ═══════════════════════════════════════════════════════════════
# 安全不变量：内存持真实值后，YAML 仍只存 ${REF}、明文只落 secrets.env
# ═══════════════════════════════════════════════════════════════


class TestPostProviderPersistenceInvariant:

    def test_yaml_keeps_ref_not_plaintext(self, tmp_path):
        pm, cd, cfg_path, secrets_path = _make_pm(tmp_path)
        h = _make_handler("POST", {
            "name": "openai-x",
            "api_key": "sk-secret-999",
            "openai_base": "https://api.x.com/v1",
        })
        h._handle_cloud_providers(pm)

        saved = yaml.safe_load(cfg_path.read_text())
        p = saved["providers"]["openai-x"]
        # YAML 里 api_key 是 ${REF}，不是明文
        assert p["api_key"].startswith("${"), "YAML must store the ${REF}, not plaintext"
        assert p["api_key"] == "${IFF_OPENAI_X_KEY}"
        assert "sk-secret-999" not in cfg_path.read_text()

        # 明文只落 secrets.env（0600）
        assert "sk-secret-999" in secrets_path.read_text()
        assert oct(secrets_path.stat().st_mode & 0o777) == oct(0o600)
