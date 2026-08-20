"""API 规范加载器 — 加载 OpenAPI YAML 并提供给 handler。"""

import logging
import os
import yaml
from pathlib import Path

log = logging.getLogger(__name__)

_SPEC_PATH = Path(__file__).resolve().parent.parent / "api-spec" / "openapi.yaml"
_spec: dict | None = None
_mtime: float = 0.0


def get_openapi_spec() -> dict:
    """获取 OpenAPI 规范字典（自动缓存 + 版本注入）。"""
    global _spec, _mtime
    try:
        current_mtime = _SPEC_PATH.stat().st_mtime
    except FileNotFoundError:
        log.warning("OpenAPI spec not found: %s", _SPEC_PATH)
        return {"error": "OpenAPI spec not found"}
    
    if _spec is None or current_mtime != _mtime:
        with open(_SPEC_PATH) as f:
            raw = yaml.safe_load(f)
        from . import __version__
        raw["info"]["version"] = __version__
        _spec = raw
        _mtime = current_mtime
    
    return _spec
