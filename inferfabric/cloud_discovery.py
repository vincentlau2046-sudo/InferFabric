"""云端 Provider 自动发现与配置管理

从 cloud_provider.yaml 加载 provider 配置，
通过 GET <openai_base>/models 自动发现可用模型，
构建 CloudModel 注册表供路由决策使用。

v4.6.0: 模型能力属性 (context_window, max_output_tokens 等)
  - cloud_provider.yaml 的 models: 段可手动指定
  - 自动发现结果与手动配置合并（手动优先）
"""

import logging
import os
import re
import time
import threading
import json as _json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
import urllib.request
import urllib.error

log = logging.getLogger("inferfabric.cloud_discovery")


@dataclass
class CloudModel:
    """云端模型条目。"""
    model_id: str
    provider: str
    openai_available: bool = True
    anthropic_available: bool = False
    discovered_at: float = 0.0
    # v4.6.0: 模型能力属性 — 对齐 OpenClaw model spec
    name: str = ""                   # 人类可读名称, e.g. "DeepSeek V4 Flash"
    context_window: int | None = None       # 模型理论最大上下文
    max_output_tokens: int | None = None    # 模型理论最大输出
    contextWindow: int | None = None        # 实际可用上下文 (考虑流控)
    maxTokens: int | None = None            # 实际建议最大输出 (考虑流控)
    input: list[str] = field(default_factory=lambda: ["text"])  # 输入模态
    reasoning: bool = False           # 是否支持思考模式
    supports_vision: bool = False
    supports_tools: bool = False
    # G-2: 价格字段 (¥/1M tokens)
    price_input: float = 0.0
    price_output: float = 0.0
    # 自定义扩展字段 (YAML 中的额外 key-value)
    extra: dict = field(default_factory=dict)

    def to_api_dict(self) -> dict:
        """转换为 OpenAI /v1/models 响应格式的 dict。
        
        字段对齐 OpenClaw openclaw.json model spec:
        - contextWindow / maxTokens: 实际可用值 (考虑流控)
        - context_window / max_output_tokens: 模型理论最大值
        """
        d = {
            "id": self.model_id,
            "object": "model",
            "owned_by": f"cloud:{self.provider}",
            "permission": [],
        }
        if self.name:
            d["name"] = self.name
        # 填充能力属性
        caps = {}
        # 实际可用值 (client 优先使用)
        if self.contextWindow is not None:
            caps["contextWindow"] = self.contextWindow
        if self.maxTokens is not None:
            caps["maxTokens"] = self.maxTokens
        # 模型理论最大值
        if self.context_window is not None:
            caps["context_window"] = self.context_window
        if self.max_output_tokens is not None:
            caps["max_output_tokens"] = self.max_output_tokens
        # 模态与能力
        if self.input:
            caps["input"] = self.input
        caps["reasoning"] = self.reasoning
        caps["supports_vision"] = self.supports_vision
        caps["supports_tools"] = self.supports_tools
        # 自定义扩展
        if self.extra:
            caps.update(self.extra)
        d["capabilities"] = caps
        return d


@dataclass
class ProviderConfig:
    """单个 cloud provider 的配置。"""
    name: str
    api_key: str = ""
    openai_base: str = ""
    anthropic_base: str = ""
    timeout: int = 60
    enabled: bool = True
    # Discovery
    discovery_enabled: bool = True
    discovery_endpoint: str = "/models"
    discovery_interval: int = 3600
    include_pattern: str = ""
    # Routing
    routing_default: str = "cloud_only"
    # v4.6.0: 模型能力属性手动覆盖
    model_specs: dict[str, dict] = field(default_factory=dict)


class CloudDiscovery:
    """云端模型发现引擎。

    用法：
        cd = CloudDiscovery(Path("cloud_provider.yaml"))
        models = cd.discover_all()   # dict[str, CloudModel]
    """

    def __init__(self, config_path: Path | None = None):
        self._providers: dict[str, ProviderConfig] = {}
        self._cloud_models: dict[str, CloudModel] = {}
        self._models_lock = threading.RLock()  # protects _cloud_models + _providers reads/writes
        # 锁序约定: _save_lock → _models_lock (reload 嵌套时)。handler add/delete: _models_lock → save_config(_save_lock)，不嵌套。
        # G-2: price config cached for metrics_aggregator
        self._price_config: dict[str, tuple[float, float]] = {}
        self._last_discovery: float = 0.0
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._config_path: Path | None = config_path
        self._save_lock = threading.Lock()
        self._config_corrupt = False
        if config_path:
            self._load_config(config_path)
            # Register spec-only models on startup so they're visible before first discover
            self._register_spec_only_models(self._cloud_models)

    @property
    def providers(self) -> dict[str, ProviderConfig]:
        return self._providers

    @property
    def cloud_models(self) -> dict[str, CloudModel]:
        with self._models_lock:
            return dict(self._cloud_models)  # return snapshot, not mutable reference

    def discover_all(self) -> dict[str, CloudModel]:
        """对所有 enabled provider 执行模型发现。返回合并后的模型注册表。"""
        merged: dict[str, CloudModel] = {}
        for name, cfg in self._providers.items():
            if not cfg.enabled or not cfg.discovery_enabled:
                continue
            try:
                models = self._discover_provider(cfg)
                for m in models:
                    # 合并手动 model_specs
                    m = self._merge_model_spec(m, cfg)
                    # 同一 model_id 多 provider 时：短名 key 指向首个，
                    # 同时存 <provider>/<model_id> 形式的带前缀 key
                    if m.model_id not in merged:
                        merged[m.model_id] = m
                    merged[f"{m.provider}/{m.model_id}"] = m
                log.info("Provider %s: discovered %d models", name, len(models))
            except Exception as e:
                log.warning("Provider %s discovery failed: %s", name, e)
        # 也注册仅有 model_specs 但未被发现的模型
        self._register_spec_only_models(merged)
        # Preserve old models if new discovery yields nothing (network blip protection)
        if not merged and self._cloud_models:
            log.warning("Discovery yielded 0 models — keeping previous %d models",
                        len(self._cloud_models))
            return dict(self._cloud_models)
        with self._models_lock:
            self._cloud_models = merged
        self._last_discovery = time.time()
        return merged

    def start_polling(self):
        """启动后台轮询（非阻塞）。"""
        if self._poll_thread and self._poll_thread.is_alive():
            return
        intervals = [cfg.discovery_interval for cfg in self._providers.values()
                     if cfg.enabled and cfg.discovery_enabled and cfg.discovery_interval > 0]
        if not intervals:
            return
        interval = min(intervals)
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, args=(interval,), daemon=True)
        self._poll_thread.start()
        log.info("Cloud discovery polling started, interval=%ds", interval)

    def stop_polling(self):
        """停止后台轮询。"""
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=10)
            self._poll_thread = None
        log.info("Cloud discovery polling stopped")

    def resolve_route(self, model_name: str, local_models: set[str]) -> Optional[str]:
        """路由决策：给定模型名，返回 "local" / "cloud:<provider>" / None。"""
        short_name = model_name.split("/")[-1] if "/" in model_name else model_name
        if short_name in local_models or model_name in local_models:
            return "local"
        with self._models_lock:
            cm = self._cloud_models.get(short_name) or self._cloud_models.get(model_name)
        if cm:
            return f"cloud:{cm.provider}"
        return None

    def get_provider_config(self, provider_name: str) -> ProviderConfig | None:
        return self._providers.get(provider_name)

    def reload(self, config_path: Path):
        """热加载配置。"""
        self.stop_polling()
        with self._save_lock:
            self._config_path = config_path
        with self._models_lock:
            self._providers = {}
        self._load_config(config_path)

    # ── internal ──

    def save_config(self):
        """原子写入当前 providers 到 cloud_provider.yaml

        流程: write-to-temp → 校验 → os.replace (POSIX 原子)
        """
        if not self._config_path:
            log.warning("No config path set, cannot save")
            return
        with self._save_lock:
            try:
                tmp = self._config_path.with_suffix('.yaml.tmp')
                data = self._serialize_providers()
                with open(tmp, 'w') as f:
                    yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
                    f.flush()
                    os.fsync(f.fileno())
                # 校验：读回确认可解析
                with open(tmp) as f:
                    yaml.safe_load(f)
                # 原子替换
                os.replace(tmp, self._config_path)
                log.info("Saved cloud provider config to %s", self._config_path)
                self._config_corrupt = False  # 成功保存后重置
            except Exception as e:
                log.error("Failed to save cloud provider config: %s", e)
                # 清理 tmp
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                raise

    def _serialize_providers(self) -> dict:
        """序列化当前 providers 为 YAML-compatible dict"""
        result = {"providers": {}}
        for name, p in self._providers.items():
            pd = {
                "openai_base": p.openai_base or "",
                "api_key": p.api_key or "",
            }
            if p.anthropic_base:
                pd["anthropic_base"] = p.anthropic_base
            if p.timeout and p.timeout != 60:
                pd["timeout"] = p.timeout
            if not p.enabled:
                pd["enabled"] = False
            if not p.discovery_enabled:
                pd["discovery_enabled"] = False
            if p.discovery_endpoint and p.discovery_endpoint != "/models":
                pd["discovery_endpoint"] = p.discovery_endpoint
            if p.discovery_interval and p.discovery_interval != 3600:
                pd["discovery_interval"] = p.discovery_interval
            if p.include_pattern:
                pd["include_pattern"] = p.include_pattern
            if p.routing_default:
                pd["routing_default"] = p.routing_default
            # 序列化 models
            if p.model_specs:
                models = {}
                for mid, spec in p.model_specs.items():
                    md = {}
                    if mid:
                        md["model_id"] = mid
                    if spec.get("price_input"):
                        md["price_input"] = spec["price_input"]
                    if spec.get("price_output"):
                        md["price_output"] = spec["price_output"]
                    # Include other spec fields beyond the known capability keys
                    known = {"name", "contextWindow", "maxTokens", "input", "reasoning",
                             "context_window", "max_output_tokens", "supports_vision", "supports_tools",
                             "price_input", "price_output"}
                    for k, v in spec.items():
                        if k not in known and k not in md:
                            md[k] = v
                    if md:
                        # key 用 short name
                        key = mid.split("/")[-1] if "/" in mid else mid
                        models[key] = md
                if models:
                    pd["models"] = models
            result["providers"][name] = pd
        return result

    @staticmethod
    def _merge_model_spec(model: CloudModel, cfg: ProviderConfig) -> CloudModel:
        """将 ProviderConfig.model_specs 中的手动属性合并到 CloudModel。

        手动配置优先：如果 model_specs 中有该模型，覆盖/补充其能力属性。
        """
        spec = cfg.model_specs.get(model.model_id)
        if not spec:
            return model
        # 新字段
        if spec.get("name"):
            model.name = spec["name"]
        if spec.get("contextWindow") is not None:
            model.contextWindow = spec["contextWindow"]
        if spec.get("maxTokens") is not None:
            model.maxTokens = spec["maxTokens"]
        if spec.get("input") is not None:
            model.input = list(spec["input"])
        if spec.get("reasoning") is not None:
            model.reasoning = bool(spec["reasoning"])
        # 原有字段
        if spec.get("context_window") is not None:
            model.context_window = spec["context_window"]
        if spec.get("max_output_tokens") is not None:
            model.max_output_tokens = spec["max_output_tokens"]
        if spec.get("supports_vision") is not None:
            model.supports_vision = bool(spec["supports_vision"])
        if spec.get("supports_tools") is not None:
            model.supports_tools = bool(spec["supports_tools"])
        # G-2: 价格字段
        if spec.get("price_input") is not None:
            model.price_input = float(spec["price_input"])
        if spec.get("price_output") is not None:
            model.price_output = float(spec["price_output"])
        # 收集非标准字段到 extra
        known = {"name", "contextWindow", "maxTokens", "input", "reasoning",
                 "context_window", "max_output_tokens", "supports_vision", "supports_tools",
                 "price_input", "price_output"}
        for k, v in spec.items():
            if k not in known:
                model.extra[k] = v
        return model

    def _register_spec_only_models(self, merged: dict[str, CloudModel]):
        """注册仅有 model_specs 但未被 /models 发现的模型。

        场景：模型暂时下线但配置已知，仍需在注册表中保留。
        """
        for name, cfg in self._providers.items():
            if not cfg.enabled:
                continue
            for mid, spec in cfg.model_specs.items():
                if mid in merged:
                    continue  # 已发现，跳过
                # 仅 spec 注册，协议信息从 provider 配置推断
                m = CloudModel(
                    model_id=mid,
                    provider=name,
                    openai_available=bool(cfg.openai_base),
                    anthropic_available=bool(cfg.anthropic_base),
                    discovered_at=0.0,  # 0 表示未实际发现，仅配置
                )
                m = self._merge_model_spec(m, cfg)
                merged[mid] = m
                merged[f"{name}/{mid}"] = m
                log.debug("Registered spec-only model: %s/%s", name, mid)

    def _load_config(self, config_path: Path):
        if not config_path.exists():
            log.info("cloud_provider.yaml not found — cloud discovery disabled")
            return
        try:
            with open(config_path) as f:
                raw = f.read()
            # Expand environment variables (${VAR} syntax)
            import re as _re
            def _env_replace(m):
                var = m.group(1)
                val = os.environ.get(var)
                if not val:
                    log.warning("cloud_provider.yaml: env var $%s not set", var)
                return val or ""
            expanded = _re.sub(r"\$\{(\w+)\}", _env_replace, raw)
            cfg = yaml.safe_load(expanded)
        except Exception as e:
            log.error("Failed to load cloud_provider.yaml: %s", e)
            self._config_corrupt = True
            return
        if not cfg:
            return

        for name, pcfg in (cfg.get("providers") or {}).items():
            if not isinstance(pcfg, dict):
                continue
            discovery = pcfg.get("discovery") or {}
            routing = pcfg.get("routing") or {}
            # v4.6.0: Parse model specs
            model_specs = {}
            for mid, mspec in (pcfg.get("models") or {}).items():
                if isinstance(mspec, dict):
                    model_specs[mid] = mspec
            provider = ProviderConfig(
                name=name,
                api_key=pcfg.get("api_key", ""),
                openai_base=pcfg.get("openai_base", ""),
                anthropic_base=pcfg.get("anthropic_base", ""),
                timeout=pcfg.get("timeout", 60),
                enabled=pcfg.get("enabled", True),
                discovery_enabled=discovery.get("enabled", True),
                discovery_endpoint=discovery.get("endpoint", "/models"),
                discovery_interval=discovery.get("interval", 3600),
                include_pattern=discovery.get("filter", {}).get("include_pattern", ""),
                routing_default=routing.get("default", "cloud_only"),
                model_specs=model_specs,
            )
            self._providers[name] = provider

        log.info("Cloud config loaded: %d providers, %d model specs",
                 len(self._providers),
                 sum(len(p.model_specs) for p in self._providers.values()))
        self._config_corrupt = False  # 成功加载后重置 corrupt 标志

    def _discover_provider(self, cfg: ProviderConfig) -> list[CloudModel]:
        """对单个 provider 执行 GET /models。"""
        if not cfg.openai_base:
            return []

        url = f"{cfg.openai_base.rstrip('/')}{cfg.discovery_endpoint}"
        log.debug("Discovering models from %s: %s", cfg.name, url)

        req = urllib.request.Request(url)
        if cfg.api_key:
            req.add_header("Authorization", f"Bearer {cfg.api_key}")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                body = _json.loads(resp.read().decode('utf-8')) or {}
        except urllib.error.HTTPError as e:
            log.warning("Provider %s returned HTTP %d", cfg.name, e.code)
            return []
        except Exception as e:
            log.warning("Provider %s request failed: %s", cfg.name, e)
            return []

        raw_models = body.get("data") or []
        pattern = re.compile(cfg.include_pattern) if cfg.include_pattern else None
        has_anthropic = bool(cfg.anthropic_base)

        results = []
        for m in raw_models:
            mid = m.get("id", "")
            if not mid:
                continue
            if pattern and not pattern.match(mid):
                continue
            results.append(CloudModel(
                model_id=mid,
                provider=cfg.name,
                openai_available=True,
                anthropic_available=has_anthropic,
                discovered_at=time.time(),
            ))

        return results

    def _poll_loop(self, interval: int):
        """后台轮询循环。"""
        while not self._stop_event.wait(timeout=interval):
            try:
                self.discover_all()
            except Exception as e:
                log.warning("Polling discovery error: %s", e)
