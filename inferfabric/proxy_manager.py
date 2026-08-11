"""
inferfabric/proxy_manager.py — Model switching, health check, and request routing.

Extracted from proxy.py for modularity.
"""

import itertools
import json as _json
import queue as _queue
import uuid
import logging
import os
import threading
import time
from http.client import HTTPConnection
from typing import Optional

import yaml as _yaml

from inferfabric.manager import ModelManager
from inferfabric.state import GPUMode
from inferfabric.config import MODELS_DIR, DEFAULT_REQUEST_LOG_DB, ConfigError
from inferfabric.proxy.auth import AuthManager
from inferfabric.proxy.request_logger import RequestLogger, RequestLog
from inferfabric.cloud_discovery import CloudDiscovery, CloudModel
from inferfabric.ratelimit import DualGateLimiter, RateLimiterV2
from inferfabric.metrics_aggregator import MetricsAggregator, AggregatorThread, CloudModelPrice
from inferfabric.request_log_db import RequestLogDB
from pathlib import Path as _Path

# IFF data directory (consistent with config.py / token_stats.py)
from inferfabric.config import IFF_DATA_DIR

log = logging.getLogger("inferfabric.proxy_manager")


# ─── Config ──────────────────────────────────────────────────────

PROXY_HOST = os.environ.get("EDGE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("EDGE_PROXY_PORT", "8999"))
AUTO_SWITCH = os.environ.get("EDGE_AUTO_SWITCH", "1") == "1"
HEALTH_CHECK_INTERVAL = int(os.environ.get("EDGE_HEALTH_CHECK", "60"))
WATCHDOG_INTERVAL = 20


class ProxyManager:
    """Manages model switching + request routing (v4.0: model-plugin)."""

    def __init__(self, mgr: Optional["ModelManager"] = None, models_dir: str | None = None):
        self.mgr = mgr if mgr is not None else ModelManager(models_dir or str(MODELS_DIR))
        self._last_switch = 0.0
        self._cooldown = 10
        self._switch_lock = threading.Lock()
        # PR-A: Auth manager
        self.auth = AuthManager(IFF_DATA_DIR / "api_keys.yaml")
        # PR-D: Cloud discovery (must init before aggregator for price config)
        self.cloud = CloudDiscovery(IFF_DATA_DIR / "cloud_provider.yaml")
        self._cloud_discovered = False
        # v4.6.2: Runtime config (iff.yaml overrides)
        self._runtime_config = self._load_runtime_config()
        # v4.6.3: DualGateLimiter — 可配置流控 (PR-G4/G1/G3)
        rate_cfg = self._runtime_config.get("rate_limit", {})
        rate_mode = rate_cfg.get("mode", "observe")
        server_rpm = rate_cfg.get("server_rpm", 0)
        model_rpm_default = rate_cfg.get("model_rpm_default", 0)
        rate_timeout = rate_cfg.get("timeout", 5)
        max_concurrent_cfg = rate_cfg.get("max_concurrent", "auto")
        if max_concurrent_cfg == "auto":
            max_concurrent = self._compute_max_concurrent()
        else:
            max_concurrent = int(max_concurrent_cfg)
        self.dual_gate = DualGateLimiter(
            rpm_limiter=RateLimiterV2(
                server_rpm=server_rpm,
                model_rpm_default=model_rpm_default,
                timeout=rate_timeout,
            ),
            max_concurrent=max_concurrent,
            mode=rate_mode,
            timeout=rate_timeout,
        )
        log.info(
            "Rate limit: mode=%s server_rpm=%s model_rpm_default=%s max_concurrent=%d timeout=%ds",
            rate_mode, server_rpm, model_rpm_default, max_concurrent, rate_timeout,
        )
        # G-2 + v4.6.2: Metrics aggregator (queue-decoupled) + SQLite replay
        self._reqlog_db = RequestLogDB(DEFAULT_REQUEST_LOG_DB)
        self._agg_queue = _queue.Queue()
        # D-2: Build served_name → friendly_name mapping for dashboard
        self._metrics_name_map = {}
        for m in self.mgr._models.values():
            sn = m.served_name
            if sn and sn != m.name:
                self._metrics_name_map[sn] = m.name
        self.metrics = MetricsAggregator(db=self._reqlog_db, replay_hours=720.0,
                                          model_name_map=self._metrics_name_map)
        self._agg_thread = AggregatorThread(self.metrics, self._agg_queue)
        self._agg_thread.start()
        # PR-B + v4.6.2: Request logger (feeds aggregator via queue + SQLite)
        self.logger = RequestLogger(log_dir=IFF_DATA_DIR / "logs", enabled=True,
                                     on_log_queue=self._agg_queue,
                                     db=self._reqlog_db,
                                     jsonl_enabled=self._runtime_config.get("access_log_jsonl", True),
                                     retention_days=self._runtime_config.get("request_log_retention_days", 90))
        # PR-B: Helper to create request context
        self._req_counter = itertools.count()

    def new_request_id(self) -> str:
        """Generate a unique, thread-safe request ID for logging.

        Format: {8-hex-counter}-{8-hex-uuid} (e.g. ``00000001-a3b4f2c1``).
        The atomic counter guarantees uniqueness across threads; the random
        suffix further eliminates any risk of collision across process restarts.
        """
        return f"{next(self._req_counter):08x}-{uuid.uuid4().hex[:8]}"

    def ensure_cloud_discovered(self):
        """首次请求时触发云端模型发现（懒加载）+ 启动后台轮询。"""
        if not self._cloud_discovered and self.cloud.providers:
            self.cloud.discover_all()
            self._cloud_discovered = True
            log.info("Cloud discovery completed: %d models", len(self.cloud.cloud_models))
            # Start background polling for model updates
            self.cloud.start_polling()
            # G-2: Update price config now that cloud models are available
            self.metrics.update_prices(self._load_price_config())

    def _compute_max_concurrent(self) -> int:
        """从 vLLM 模型配置中取 max_num_seqs 最大值作为并发上限。

        确保 IFF 的并发限制不低于 vLLM 的处理能力，避免人为瓶颈。
        仅考虑本地 vLLM 模型（cloud 模型不走本地并发门）。
        """
        max_seqs = 4  # 保守默认
        for model in self.mgr._models.values():
            if model.is_vllm and model.vllm:
                max_seqs = max(max_seqs, model.vllm.max_num_seqs)
        log.debug("Computed max_concurrent=%d from vLLM configs", max_seqs)
        return max_seqs

    def _validate_runtime_config(self, config: dict):
        """Validate iff.yaml schema. Raises ConfigError on invalid values.

        Required fields and constraints:
          - rate_limit.mode: "observe" or "reject"
          - rate_limit.timeout: int > 0
          - rate_limit.server_rpm: int >= 0
          - rate_limit.model_rpm_default: int >= 0
          - rate_limit.max_concurrent: "auto" or int > 0
          - access_log_jsonl: bool
          - request_log_retention_days: int > 0
          - tts.enabled: bool (optional)
          - tts.port: int > 0 (optional)
          - asr.enabled: bool (optional)
          - asr.port: int > 0 (optional)
        """
        from inferfabric.config import ConfigError

        rate_cfg = config.get("rate_limit", {})
        if not isinstance(rate_cfg, dict):
            raise ConfigError("rate_limit must be a mapping")

        mode = rate_cfg.get("mode", "observe")
        if mode not in ("observe", "reject"):
            raise ConfigError(f"rate_limit.mode must be 'observe' or 'reject', got {mode!r}")

        timeout = rate_cfg.get("timeout", 5)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ConfigError(f"rate_limit.timeout must be int > 0, got {timeout!r}")

        server_rpm = rate_cfg.get("server_rpm", 0)
        if isinstance(server_rpm, bool) or not isinstance(server_rpm, int) or server_rpm < 0:
            raise ConfigError(f"rate_limit.server_rpm must be int >= 0, got {server_rpm!r}")

        model_rpm_default = rate_cfg.get("model_rpm_default", 0)
        if isinstance(model_rpm_default, bool) or not isinstance(model_rpm_default, int) or model_rpm_default < 0:
            raise ConfigError(f"rate_limit.model_rpm_default must be int >= 0, got {model_rpm_default!r}")

        max_concurrent = rate_cfg.get("max_concurrent", "auto")
        if isinstance(max_concurrent, bool):
            raise ConfigError(f"rate_limit.max_concurrent must be 'auto' or int > 0, got {max_concurrent!r}")
        elif isinstance(max_concurrent, str):
            if max_concurrent != "auto":
                raise ConfigError(f"rate_limit.max_concurrent must be 'auto' or int > 0, got {max_concurrent!r}")
        elif isinstance(max_concurrent, int):
            if max_concurrent <= 0:
                raise ConfigError(f"rate_limit.max_concurrent must be int > 0, got {max_concurrent}")
        else:
            raise ConfigError(f"rate_limit.max_concurrent must be 'auto' or int, got {type(max_concurrent).__name__}")

        if "access_log_jsonl" in config:
            if not isinstance(config["access_log_jsonl"], bool):
                raise ConfigError(f"access_log_jsonl must be bool, got {config['access_log_jsonl']!r}")

        if "request_log_retention_days" in config:
            rd = config["request_log_retention_days"]
            if isinstance(rd, bool) or not isinstance(rd, int) or rd <= 0:
                raise ConfigError(f"request_log_retention_days must be int > 0, got {rd!r}")

        # Validate asr/tts local service config
        for svc_name in ("asr", "tts"):
            svc_cfg = config.get(svc_name)
            if svc_cfg is None:
                continue
            if not isinstance(svc_cfg, dict):
                raise ConfigError(f"{svc_name} must be a mapping, got {type(svc_cfg).__name__}")
            if "enabled" in svc_cfg and not isinstance(svc_cfg["enabled"], bool):
                raise ConfigError(f"{svc_name}.enabled must be bool, got {svc_cfg['enabled']!r}")
            if "port" in svc_cfg:
                p = svc_cfg["port"]
                if isinstance(p, bool) or not isinstance(p, int) or p <= 0:
                    raise ConfigError(f"{svc_name}.port must be int > 0, got {p!r}")

    def _load_runtime_config(self) -> dict:
        """从 iff.yaml 加载运行时配置，不存在时返回空 dict。

        支持的配置项:
          - access_log_jsonl: bool (默认 True)
          - request_log_retention_days: int (默认 90)
          - rate_limit.mode: "observe" | "reject" (默认 observe)
          - rate_limit.server_rpm: int (默认 0=不限流)
          - rate_limit.model_rpm_default: int (默认 0=不限流)
          - rate_limit.max_concurrent: "auto" | int (默认 auto)
          - rate_limit.timeout: int (默认 5)
        """
        config_path = IFF_DATA_DIR / "iff.yaml"
        if not config_path.exists():
            return {}
        try:
            with open(config_path) as f:
                cfg = _yaml.safe_load(f)
            if not cfg or not isinstance(cfg, dict):
                return {}
            self._validate_runtime_config(cfg)
            return cfg
        except ConfigError as e:
            log.warning("Invalid iff.yaml configuration: %s — using defaults", e)
            return {}
        except Exception:
            log.warning("Failed to load iff.yaml — using defaults", exc_info=True)
            return {}

    def _load_price_config(self) -> dict[str, CloudModelPrice]:
        """从 cloud_provider.yaml 加载价格配置（cloud_models + provider model_specs）"""
        prices = {}
        try:
            if hasattr(self, 'cloud') and self.cloud:
                # 优先从 CloudModel 实例读取（含 spec-only 注册模型）
                for model_id, model in self.cloud.cloud_models.items():
                    if "/" in model_id:
                        continue  # 跳过 provider-prefixed 键
                    if model.price_input > 0 or model.price_output > 0:
                        prices[model_id] = CloudModelPrice(
                            price_input=model.price_input,
                            price_output=model.price_output,
                        )
                # 回退：从 provider model_specs 补充（尚未注册为 CloudModel 的）
                for _pname, pcfg in self.cloud._providers.items():
                    for mid, spec in pcfg.model_specs.items():
                        if mid in prices:
                            continue
                        pi = spec.get("price_input", 0)
                        po = spec.get("price_output", 0)
                        if pi > 0 or po > 0:
                            prices[mid] = CloudModelPrice(
                                price_input=float(pi),
                                price_output=float(po),
                            )
        except Exception as e:
            log.warning("Failed to load price config: %s", e)
        return prices

    @property
    def current(self) -> str:
        """Current active service or 'idle'."""
        return self.mgr.current_service

    def model_to_service(self, model_name: str):
        """Map served_model_name to model config name."""
        m = self.mgr.find_model_by_served_name(model_name)
        if m:
            log.debug("model_to_service: %s → %s", model_name, m.name)
            return m.name
        return None

    def _wait_healthy(self, target: str, timeout: float = 180) -> bool:
        """Wait for a model to become healthy after switch."""
        model = self.mgr.get_model(target)
        if not model:
            return False
        port = model.port
        if not port:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            conn = None
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                resp.read()
                if resp.status == 200:
                    conn.close()
                    log.info("Model %s healthy on :%d", target, port)
                    return True
                resp.close()
            except Exception:
                pass
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
            time.sleep(2)
        log.warning("Model %s not healthy after %.0fs", target, timeout)
        return False

    def ensure_service(self, target: str) -> bool:
        """Ensure a model is running, auto-switch if needed."""
        if target in self.mgr.active_services:
            return True
        if self.mgr.state.is_manually_stopped(target):
            log.info("Auto-switch to %s blocked: manually stopped by user", target)
            return False
        if not self._switch_lock.acquire(timeout=0):
            log.warning("Switch already in progress, rejecting")
            return None  # caller should send 409
        try:
            if time.time() - self._last_switch < self._cooldown:
                log.warning("Switch cooldown active, skipping")
                return False
            log.info("Auto-switch → %s", target)
            result = self.mgr.switch(target)
            ok = result["status"] == "switched"
            if ok:
                self._last_switch = time.time()
                return self._wait_healthy(target)
            return result["status"] in ("switched", "already_active")
        finally:
            self._switch_lock.release()

    def get_target_port(self, model_name: str):
        """Get port for a served_model_name."""
        m = self.mgr.find_model_by_served_name(model_name)
        return m.port if m else None

    def make_conn(self, port: int, timeout: int = 300) -> HTTPConnection:
        """Create new HTTP connection per request — no pool (thread-safe).

        Each thread gets its own connection to vLLM, avoiding race conditions.
        vLLM handles concurrent connections natively.
        """
        return HTTPConnection("127.0.0.1", port, timeout=timeout)

    def health_check(self):
        try:
            s = self.mgr.status()
            self.mgr.cleanup_dead_services()
            log.info("Health check: gpu_mode=%s services=%s",
                     s.get("gpu_mode"), s.get("active_services"))
            for svc, health in s.get("services_health", {}).items():
                if health == "❌" and s.get("gpu_mode") != GPUMode.IDLE:
                    log.warning("%s unhealthy but GPU not idle — use `iff reconcile`", svc)
            self._clean_manual_stops()
        except Exception as e:
            log.error("Health check exception: %s", e)

    def _clean_manual_stops(self):
        """Remove expired manual_stop records from StateDB."""
        try:
            stops = _json.loads(self.mgr.state.get("manual_stops") or "{}")
            expired = [k for k, v in stops.items() if time.time() - v > self.mgr.state.MANUAL_STOP_TTL]
            if expired:
                for k in expired:
                    del stops[k]
                self.mgr.state.set("manual_stops", _json.dumps(stops))
                log.debug("Cleaned %d expired manual_stop records", len(expired))
        except Exception as e:
            log.debug("Manual stop cleanup error: %s", e)
