"""单用户鉴权 — Bearer token 校验 + 可选临时 key

配置文件 api_keys.yaml:
  primary: "sk-iff-xxx"          # 主 key，长期有效，全模型
  guests:                        # 可选
    - key: "***"
      name: "测试用"
      models: ["qwen35-9b"]
      expires: "2026-08-30T00:00:00+08:00"

文件不存在或为空 = 不开启鉴权（当前行为不变）。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import logging

import yaml

log = logging.getLogger("inferfabric.auth")


@dataclass
class _KeyEntry:
    key: str
    name: str
    models: list[str] | None = None   # None = 全部模型
    expires: datetime | None = None


class AuthManager:
    """单用户鉴权管理器。

    - api_keys.yaml 不存在或为空 → 不启用鉴权
    - primary key: 长期有效，全模型
    - guest keys: 可选，模型白名单 + 过期时间
    """

    def __init__(self, config_path: Path | None = None):
        self._primary: _KeyEntry | None = None
        self._guests: list[_KeyEntry] = []
        self._key_map: dict[str, _KeyEntry] = {}
        if config_path:
            self._load(config_path)

    @property
    def enabled(self) -> bool:
        return self._primary is not None

    def check(self, bearer_token: str, model: str) -> tuple[bool, str]:
        """校验 Bearer token + 模型访问权限。

        Returns:
            (通过, 原因) — True/False + 人类可读原因
        """
        token = self._strip_bearer(bearer_token)
        entry = self._key_map.get(token)
        if not entry:
            return False, "invalid key"
        if entry.expires:
            # Guard: if expires is timezone-naive, treat as UTC
            exp = entry.expires
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
                log.warning("Guest key '%s' has timezone-naive expiry, treating as UTC", entry.name)
            if datetime.now(tz=timezone.utc) > exp:
                return False, "key expired"
        if entry.models is not None and model not in entry.models:
            return False, f"model '{model}' not allowed for key '{entry.name}'"
        return True, "ok"

    def key_name(self, bearer_token: str) -> str:
        """获取 key 名称（用于日志记录）。"""
        token = self._strip_bearer(bearer_token)
        entry = self._key_map.get(token)
        return entry.name if entry else "anonymous"

    def reload(self, config_path: Path):
        """热加载配置（iff reload 触发）。"""
        self._primary = None
        self._guests = []
        self._key_map = {}
        self._load(config_path)

    # ── internal ──

    @staticmethod
    def _strip_bearer(header: str) -> str:
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return header.strip()

    def _load(self, config_path: Path):
        if not config_path.exists():
            log.info("api_keys.yaml not found — auth disabled")
            return
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
        except Exception as e:
            log.error("Failed to load api_keys.yaml: %s", e)
            return
        if not cfg or not isinstance(cfg, dict):
            log.warning("api_keys.yaml has no valid config (empty or wrong format) — auth DISABLED")
            return

        # Primary key
        primary_key = cfg.get("primary")
        if not primary_key or not isinstance(primary_key, str):
            log.warning("api_keys.yaml missing 'primary' key — auth DISABLED")
            return
        self._primary = _KeyEntry(
            key=primary_key, name="primary", models=None, expires=None,
        )
        self._key_map[primary_key] = self._primary

        # Guest keys
        for guest in cfg.get("guests") or []:
            if not isinstance(guest, dict) or not guest.get("key"):
                continue
            expires = None
            if guest.get("expires"):
                try:
                    expires = datetime.fromisoformat(guest["expires"])
                except (ValueError, TypeError):
                    log.warning(
                        "Invalid expires for guest '%s': %s",
                        guest.get("name"), guest.get("expires"),
                    )
            entry = _KeyEntry(
                key=guest["key"],
                name=guest.get("name", "guest"),
                models=guest.get("models"),
                expires=expires,
            )
            self._guests.append(entry)
            self._key_map[guest["key"]] = entry

        log.info(
            "Auth loaded: primary=%s, guests=%d",
            bool(self._primary), len(self._guests),
        )
