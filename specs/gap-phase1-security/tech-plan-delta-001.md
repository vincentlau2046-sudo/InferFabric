# GAP Phase 1 — 技术方案

> 日期: 2026-08-02 | 增量: delta-001
> 关联: spec-delta-001.md

## 修改文件清单

| 文件 | 变更 | D项 |
|------|------|-----|
| `inferfabric/proxy/handler.py` | req_id 生成 + admin token + SSRF 防护 | D-1, D-3, D-4 |
| `inferfabric/proxy_manager.py` | iff.yaml schema 校验 | D-5 |
| `inferfabric/process_manager.py` | 进程终止精确化 | D-2 |
| `inferfabric/config.py` | ConfigError 异常类（若不存在）| D-5 |
| `tests/test_gap_phase1.py` | 新增测试 | All |

## D-1 实现方案

```python
# handler.py — ProxyManager.__init__ 或类属性
import itertools

class ProxyManager:
    def __init__(self, ...):
        self._req_counter = itertools.count()
        ...

    # handler.py — new_request_id 方法
    def new_request_id(self) -> str:
        return f"{next(self._req_counter):08x}-{uuid4().hex[:8]}"
```

**线程安全论证**: CPython 的 `itertools.count.__next__` 是 C 实现，在 GIL 下等效原子。无需加锁。

## D-2 实现方案

```python
# process_manager.py — 替换 _pkill_vllm_fallback

def _kill_by_pid_file(self, pid_file: Path) -> bool:
    """通过 PID 文件精确杀进程"""
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, FileNotFoundError):
        return False
    # SIGTERM → wait → SIGKILL (单 PID)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # 已死
    for _ in range(int(STOP_SIGTERM_TIMEOUT)):
        try:
            os.kill(pid, 0)  # 检查存活
        except ProcessLookupError:
            return True
        time.sleep(1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return True

def _kill_by_port(self, port: int) -> bool:
    """fuser 精确杀占用端口的进程（最后兜底）"""
    try:
        result = subprocess.run(
            ["fuser", "-k", "-9", f"{port}/tcp"],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
```

**删除**: `_pkill_vllm_fallback` 中的 `pkill -9 -f "vllm serve"` 和 `pkill -9 -f "VLLM::EngineCore"`

**进程终止层次**: PGID kill (主) → PID file kill (fallback) → fuser port kill (兜底)

## D-3 实现方案

```python
# handler.py — 新增
import ipaddress
from urllib.parse import urlparse

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

def _validate_cloud_test_url(self, url: str) -> tuple[bool, str]:
    """校验 cloud test URL 防止 SSRF"""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False, f"Only https allowed, got {parsed.scheme}"
    # 检查白名单 host
    allowed_hosts = self._get_allowed_cloud_hosts()
    if parsed.hostname not in allowed_hosts:
        return False, f"Host '{parsed.hostname}' not in allowed list"
    # DNS 解析后检查 IP
    import socket
    try:
        ips = socket.getaddrinfo(parsed.hostname, None)
        for _, _, _, _, addr in ips:
            ip = ipaddress.ip_address(addr[0])
            for net in _PRIVATE_NETWORKS:
                if ip in net:
                    return False, f"Private IP {ip} not allowed"
    except socket.gaierror:
        return False, f"Cannot resolve host '{parsed.hostname}'"
    return True, ""

def _get_allowed_cloud_hosts(self) -> set[str]:
    """从已注册 cloud provider 获取白名单 host"""
    hosts = set()
    if hasattr(self, '_cloud_models'):
        for model in self._cloud_models.values():
            base = model.get("openai_base", "")
            if base:
                hosts.add(urlparse(base).hostname)
    return hosts
```

## D-4 实现方案

```python
# handler.py — 替换 _check_admin
import hmac

def _check_admin(self) -> bool:
    if not _ADMIN_TOKEN:
        return True  # 无 token = 开放模式（localhost 绑定兜底）
    token = self.headers.get("X-Admin-Token", "")
    return hmac.compare_digest(token, _ADMIN_TOKEN)

# proxy_manager.py — 启动时检查
def _validate_admin_security(self):
    if not _ADMIN_TOKEN and PROXY_HOST not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError(
            "IFF_ADMIN_TOKEN not set but binding to non-localhost address "
            f"'{PROXY_HOST}'. Set IFF_ADMIN_TOKEN or bind to localhost."
        )
    if not _ADMIN_TOKEN:
        log.warning("IFF_ADMIN_TOKEN not set — admin routes open (localhost-only binding)")
```

## D-5 实现方案

```python
# proxy_manager.py — 新增
def _validate_runtime_config(self, config: dict):
    """iff.yaml schema 校验 — fail-fast"""
    if not isinstance(config, dict):
        raise ConfigError(f"iff.yaml: expected dict, got {type(config).__name__}")

    rl = config.get("rate_limit", {})
    if not isinstance(rl, dict):
        raise ConfigError(f"rate_limit: expected dict, got {type(rl).__name__}")

    # mode
    mode = rl.get("mode", "observe")
    if mode not in ("observe", "reject"):
        raise ConfigError(f"rate_limit.mode: must be 'observe' or 'reject', got {mode!r}")

    # timeout
    timeout = rl.get("timeout", 5)
    if not isinstance(timeout, int) or timeout <= 0:
        raise ConfigError(f"rate_limit.timeout: expected int > 0, got {timeout!r}")

    # server_rpm
    server_rpm = rl.get("server_rpm", 0)
    if not isinstance(server_rpm, int) or server_rpm < 0:
        raise ConfigError(f"rate_limit.server_rpm: expected int >= 0, got {server_rpm!r}")

    # model_rpm_default
    model_rpm = rl.get("model_rpm_default", 0)
    if not isinstance(model_rpm, int) or model_rpm < 0:
        raise ConfigError(f"rate_limit.model_rpm_default: expected int >= 0, got {model_rpm!r}")

    # max_concurrent
    max_conc = rl.get("max_concurrent", "auto")
    if max_conc != "auto" and (not isinstance(max_conc, int) or max_conc <= 0):
        raise ConfigError(f"rate_limit.max_concurrent: expected 'auto' or int > 0, got {max_conc!r}")

    # access_log_jsonl
    alj = config.get("access_log_jsonl", True)
    if not isinstance(alj, bool):
        raise ConfigError(f"access_log_jsonl: expected bool, got {type(alj).__name__}")

    # request_log_retention_days
    rlr = config.get("request_log_retention_days", 90)
    if not isinstance(rlr, int) or rlr <= 0:
        raise ConfigError(f"request_log_retention_days: expected int > 0, got {rlr!r}")
```

## 测试策略

| D项 | 单元测试 | 集成测试 |
|-----|---------|---------|
| D-1 | 100 线程并发分配 req_id，断言无碰撞 | N/A |
| D-2 | mock Popen，验证 fallback 路径不调用 pkill -f | N/A |
| D-3 | 各类 URL 校验（私网/非白名单/合法）| N/A |
| D-4 | compare_digest 行为 + fail-fast 启动检查 | N/A |
| D-5 | 各种坏配置校验 + 正常配置通过 | N/A |

**回归**: 修改后全量 `pytest` 必须通过
