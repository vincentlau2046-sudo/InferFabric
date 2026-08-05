"""
inferfabric/proxy/handler.py — ProxyHandler, ThreadedHTTPServer, main.

Core HTTP handler with routing, dashboard, and delegation to:
  chat_handlers.py — chat completions
  metrics.py — vLLM Prometheus metrics

Extracted from proxy.py (v4.1 P3 split).
"""

import sys
import os
import signal
import socket
import logging
import json
import hmac
import ipaddress
import http.server
import socketserver
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from http.client import HTTPConnection
from concurrent.futures import ThreadPoolExecutor, as_completed

from inferfabric.state import GPUMode
from inferfabric.proxy.request_logger import RequestLog

# Admin token for control-plane routes (/switch, /stop, /deploy, /pull, etc.)
# If set, requests must include X-Admin-Token header matching this value.
# If not set (default), all control routes are open (localhost-only binding is the security boundary).
_ADMIN_TOKEN = os.environ.get("IFF_ADMIN_TOKEN", "")
from inferfabric.proxy_manager import (
    ProxyManager, AUTO_SWITCH, PROXY_HOST, PROXY_PORT,
    HEALTH_CHECK_INTERVAL, WATCHDOG_INTERVAL,
)
from inferfabric import forwarder, __version__
from inferfabric.proxy.chat_handlers import handle_chat, handle_ollama_native
from inferfabric.proxy.metrics import handle_vllm_metrics
from inferfabric.token_stats import TokenStatsCollector
from inferfabric.watchdog import ModelWatchdog

log = logging.getLogger("inferfabric.proxy")


class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug("[proxy] " + fmt, *args)

    @property
    def proxy(self):
        return self.server.proxy_mgr

    def _safe_write(self, data: bytes):
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ─── HTTP methods ─────────────────────────────────────────────

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
        except Exception:
            pass

    def do_GET(self):
        pm = self.proxy
        try:
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            if path == "/":
                self._serve_dashboard()
            elif self.path == "/health":
                self._send_json({"status": "ok", "gpu_mode": pm.mgr.gpu_mode})
            elif self.path == "/status":
                self._send_json(pm.mgr.status())
            elif self.path == "/models":
                self._send_json(pm.mgr.list_models())
            elif self.path == "/profiles":
                log.warning("/profiles endpoint is deprecated, use /models")
                self._send_json(pm.mgr.list_models())
            elif self.path == "/local-models":
                self._send_json(pm.mgr.discover_local_models())
            elif self.path == "/v1/models":
                self._handle_v1_models(pm)
            elif self.path == "/system":
                self._send_json(self._system_info())
            elif path == "/api/metrics":
                self._handle_api_metrics(pm)
            elif path == "/api/request_log":
                self._handle_request_log(pm)
            elif self.path == "/history":
                self._send_json(pm.mgr.state.get_history(limit=30))
            elif path == "/vllm_metrics":
                self._handle_vllm_metrics(pm)
            elif path == "/watchdog_status":
                wd = getattr(self.server, "watchdog", None)
                if wd:
                    self._send_json({"fail_counts": wd.fail_counts, "running": wd.running})
                else:
                    self._send_json({"error": "watchdog not initialized"}, 503)
            elif path == "/admin/cloud/providers":
                if not self._check_admin(): return
                self._handle_cloud_providers(pm)
            elif path == "/admin/cloud/presets":
                if not self._check_admin(): return
                self._handle_cloud_presets(pm)
            else:
                self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.error("GET %s error: %s", self.path, e)

    def do_POST(self):
        pm = self.proxy
        try:
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            if path in ("/v1/chat/completions", "/v1/completions"):
                self._handle_chat(pm)
            elif path == "/v1/messages":
                self._handle_messages(pm)
            elif path == "/switch":
                if not self._check_admin(): return
                self._handle_switch(pm)
            elif path == "/stop":
                if not self._check_admin(): return
                self._handle_stop(pm)
            elif path == "/sleep":
                if not self._check_admin(): return
                self._handle_sleep(pm)
            elif path == "/wake":
                if not self._check_admin(): return
                self._handle_wake(pm)
            elif path in ("/api/chat", "/api/generate"):
                self._handle_chat(pm)
            elif path == "/reset":
                if not self._check_admin(): return
                self._handle_reset(pm)
            elif path == "/reconcile":
                if not self._check_admin(): return
                self._handle_reconcile(pm)
            elif path == "/deploy":
                if not self._check_admin(): return
                self._handle_deploy(pm)
            elif path == "/pull":
                if not self._check_admin(): return
                self._handle_pull(pm)
            elif path == "/admin/cloud/reload":
                if not self._check_admin(): return
                self._handle_cloud_reload(pm)
            elif path == "/admin/cloud/discover":
                if not self._check_admin(): return
                self._handle_cloud_discover(pm)
            elif path == "/admin/cloud/test":
                if not self._check_admin(): return
                self._handle_cloud_test(pm)
            elif path == "/admin/cloud/providers":
                if not self._check_admin(): return
                self._handle_cloud_providers(pm)
            elif path == "/v1/embeddings":
                self._handle_embeddings(pm)
            elif path == "/v1/rerank":
                self._handle_rerank(pm)
            else:
                self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.error("POST %s error: %s", self.path, e)

    def do_DELETE(self):
        pm = self.proxy
        try:
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            if path == "/admin/cloud/providers":
                if not self._check_admin(): return
                self._handle_cloud_providers(pm)
            else:
                self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.error("DELETE %s error: %s", self.path, e)

    # ─── Dashboard ────────────────────────────────────────────────

    def _serve_dashboard(self):
        body = None
        try:
            from inferfabric.dashboard import get_html
            html = get_html()
            # Inject token stats (full raw state, JS filters by window)
            try:
                collector = TokenStatsCollector()
                stats_json = json.dumps(collector._load_full_state())
                # 防御 </script> 注入：json.dumps 已转义 </script>，额外移除
                stats_json = stats_json.replace('</', '<\\/')
                html = html.replace(
                    '</head>',
                    '<script>window.__TOKEN_STATS__ = ' + stats_json + ';</script></head>'
                )
            except Exception as e:
                log.warning("Failed to inject token stats: %s", e)
            body = html.encode("utf-8")
        except ImportError:
            pass
        if body is None:
            body = (
                "<!DOCTYPE html><html><head><title>InferFabric</title>"
                "<style>body{font-family:sans-serif;background:#0f1117;color:#e2e8f0;padding:24px}"
                "h1{color:#3b82f6}</style></head><body>"
                "<h1>InferFabric</h1><p>Dashboard unavailable. Use <code>iff status</code></p>"
                "</body></html>"
            ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self._safe_write(body)

    # ─── Chat ─────────────────────────────────────────────────────

    def _handle_chat(self, pm):
        data = self._read_body()
        if data is None:
            return
        handle_chat(self, pm, data)

    # ─── Anthropic Messages handler ───────────────────────────────

    def _handle_messages(self, pm):
        """Handle Anthropic Messages API requests with model-name routing.

        Routing priority (PR-2a):
          1. If profile_state == SWITCHING → 503 + Retry-After
          2. Parse `model` field → find_model_by_served_name()
             a. Model is active → route to its port
             b. Model is not active → fallback to first active LLM (backward compat)
          3. Model matches model_affinity (e.g. cloud model) → Baidu
          4. No match → fallback to first active LLM, then Baidu
        """
        data = self._read_body()
        if data is None:
            return

        original_model = data.get("model", "")
        auth_header = self.headers.get("Authorization", "") or self.headers.get("x-api-key", "")

        # PR-B: Request context for logging
        req_id = pm.new_request_id()
        req_start = time.monotonic()
        key_name = pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous"
        req_model = data.get("model", "")
        req_status = 200
        req_error = None
        req_route = "local"
        req_cloud_provider = None

        # PR-A: Auth check
        if pm.auth.enabled:
            requested_model_for_auth = req_model
            if "/" in requested_model_for_auth:
                requested_model_for_auth = requested_model_for_auth.split("/")[-1]
            auth_ok, auth_reason = pm.auth.check(auth_header, requested_model_for_auth)
            if not auth_ok:
                req_status = 401
                req_error = auth_reason
                pm.logger.log(RequestLog(
                    req_id=req_id, key_name=key_name, model=req_model,
                    status=req_status, error=req_error,
                    duration_ms=(time.monotonic()-req_start)*1000,
                ))
                self._send_json({"error": auth_reason, "status": "unauthorized"}, 401)
                return

        log.info("/v1/messages body: max_tokens=%s, model=%s, messages_count=%d, tools_count=%d, body_size=%d",
                 data.get("max_tokens"), data.get("model"),
                 len(data.get("messages", [])),
                 len(data.get("tools", [])),
                 len(json.dumps(data)))

        # PR-6e/PR-2b: SWITCHING guard — only 503 if request is NOT for the switching target
        from inferfabric.state import ServiceState
        profile_state = pm.mgr.state.get("profile_state", "")
        requested_model = data.get("model", "")
        if profile_state == ServiceState.SWITCHING:
            switching_target = pm.mgr.state.get("switching_target") or ""
            target_model = pm.mgr.find_model_by_served_name(requested_model) if requested_model else None
            if target_model and target_model.name == switching_target:
                # Request is for the switching target → let it proceed (will route once active)
                log.info("/v1/messages → target %s is switching, proceeding", switching_target)
            else:
                # Not the switching target → 503
                log.info("/v1/messages → 503 (switching to %s, not %s)", switching_target, requested_model)
                self._send_json(
                    {"error": "Model is switching, please retry", "status": "switching", "retry_after": 30},
                    503,
                    extra_headers={"Retry-After": "30"},
                )
                return

        # PR-2a: Model-name routing
        # (target_model already resolved above if SWITCHING; re-resolve only if not)
        if profile_state != ServiceState.SWITCHING:
            target_model = pm.mgr.find_model_by_served_name(requested_model) if requested_model else None

        if target_model and target_model.port and target_model.name in pm.mgr.active_services:
            # Requested model is active → route directly
            log.info("/v1/messages → LOCAL %s (port %d) [matched by model=%s]",
                     target_model.name, target_model.port, requested_model)
            self._forward_local(pm, data, auth_header, target_model, original_model)
            return

        # PR-2b: Auto-switch on demand — if model is known but not active
        if target_model and target_model.port and target_model.name not in pm.mgr.active_services:
            from inferfabric.proxy_manager import AUTO_SWITCH
            if AUTO_SWITCH:
                log.info("/v1/messages → auto-switch to %s [model=%s not active]",
                         target_model.name, requested_model)
                switched = pm.ensure_service(target_model.name)
                if switched is None:
                    self._send_json({"error": "switch already in progress", "status": "conflict"}, 409)
                    return
                if switched:
                    # Switch succeeded (ensure_service already verified healthy)
                    log.info("/v1/messages → LOCAL %s (port %d) [after auto-switch]",
                             target_model.name, target_model.port)
                    self._forward_local(pm, data, auth_header, target_model, original_model)
                    return
                else:
                    # Switch failed or model not healthy → 503
                    log.warning("/v1/messages → 503 auto-switch to %s failed", target_model.name)
                    self._send_json(
                        {"error": f"Auto-switch to {target_model.name} failed, retry later",
                         "status": "switch_failed", "retry_after": 30},
                        503,
                        extra_headers={"Retry-After": "30"},
                    )
                    return
            else:
                log.info("/v1/messages → model %s known but not active, AUTO_SWITCH=off",
                         target_model.name)
                self._send_json(
                    {"error": f"Model {target_model.name} not active, auto-switch disabled",
                     "status": "not_active", "retry_after": 30},
                    503,
                    extra_headers={"Retry-After": "30"},
                )
                return

        # Step 2 (PR-D): Unified cloud routing via CloudDiscovery
        if requested_model:
            pm.ensure_cloud_discovered()
            local_model_names = {m.served_name for m in pm.mgr._models.values() if m.served_name}
            route = pm.cloud.resolve_route(requested_model, local_model_names)
            if route and route.startswith("cloud:"):
                provider_name = route.split(":", 1)[1]
                provider_cfg = pm.cloud.get_provider_config(provider_name)
                cloud_model = pm.cloud.cloud_models.get(
                    requested_model.split("/")[-1] if "/" in requested_model else requested_model
                )
                if provider_cfg and cloud_model:
                    log.info("/v1/messages → CLOUD %s [%s] [model=%s]",
                             provider_name, "anthropic", requested_model)
                    result = forwarder.forward_to_cloud(
                        self, data, provider_cfg, cloud_model,
                        protocol="anthropic", original_model=original_model,
                    )
                    pm.logger.log(RequestLog(
                        model=original_model or requested_model, status=result.status, route=f"cloud:{provider_name}",
                        key_name=key_name, req_id=req_id,
                        cloud_provider=provider_name,
                        tokens_in=result.usage.get("prompt_tokens", 0),
                        tokens_out=result.usage.get("completion_tokens", 0),
                        ttft_ms=result.ttft_ms,
                        duration_ms=result.duration_ms,
                        error=result.error,
                    ))
                    return
                else:
                    log.warning("/v1/messages → cloud route matched but config missing: %s", route)

        # Step 3: Fallback to first active LLM (backward compat)
        active_llm = None
        for svc in pm.mgr.active_services:
            model_obj = pm.mgr.get_model(svc)
            if model_obj and model_obj.model_type in forwarder.LOCAL_LLM_TYPES:
                if model_obj.port:
                    active_llm = model_obj
                    break

        if active_llm:
            log.info("/v1/messages → LOCAL %s (port %d) [fallback: no model match for %s]",
                     active_llm.name, active_llm.port, requested_model or "<empty>")
            self._forward_local(pm, data, auth_header, active_llm, original_model)
        else:
            # PR-D: Cloud fallback (replaces old Baidu fallback)
            pm.ensure_cloud_discovered()
            if pm.cloud.cloud_models:
                # Try to find any cloud model that matches (prefer provider-prefixed key)
                short_name = requested_model.split("/")[-1] if "/" in requested_model else requested_model
                cloud_model = None
                for _key in [f"{p.name}/{short_name}" for p in pm.cloud.providers.values()] + [short_name]:
                    cloud_model = pm.cloud.cloud_models.get(_key)
                    if cloud_model:
                        break
                if cloud_model:
                    provider_cfg = pm.cloud.get_provider_config(cloud_model.provider)
                    if provider_cfg:
                        log.info("/v1/messages → CLOUD %s [fallback: no active LLM]",
                                 cloud_model.provider)
                        result = forwarder.forward_to_cloud(
                            self, data, provider_cfg, cloud_model,
                            protocol="anthropic", original_model=original_model,
                        )
                        pm.logger.log(RequestLog(
                            model=original_model or requested_model, status=result.status, route=f"cloud:{cloud_model.provider}",
                            key_name=key_name, req_id=req_id,
                            cloud_provider=cloud_model.provider,
                            tokens_in=result.usage.get("prompt_tokens", 0),
                            tokens_out=result.usage.get("completion_tokens", 0),
                            ttft_ms=result.ttft_ms,
                            duration_ms=result.duration_ms,
                            error=result.error,
                        ))
                        return
            log.info("/v1/messages → BAIDU fallback [no active LLM, no cloud match]")
            forwarder.forward_to_baidu(self, data, auth_header, original_model)

    def _forward_local(self, pm, data, auth_header, model_obj, original_model):
        """Forward request to a local model with rate limiting."""
        model_name = data.get("model", "")
        gate = pm.dual_gate.acquire(model_name, timeout=30)
        if not gate.ok:
            self._send_json(
                {"error": f"Rate limited: {gate.reason}", "status": "rate_limit"},
                429,
            )
            return
        try:
            forwarder.forward_anthropic_local(
                self, pm, data, auth_header, model_obj, original_model
            )
        finally:
            gate.release()

    # ─── v1 Models ────────────────────────────────────────────────

    def _handle_v1_models(self, pm):
        """Forward /v1/models — merge local + cloud models."""
        active = list(pm.mgr.active_services)

        def _fetch_models(svc, port):
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/v1/models")
                resp = conn.getresponse()
                body = resp.read()
                if resp.status == 200:
                    data = json.loads(body)
                    return data.get("data", [])
                return []
            except Exception as e:
                log.warning("/v1/models fetch failed for %s (port %d): %s", svc, port, e)
                return []
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        # Local models from active vLLM services (skip ollama/embedding ports)
        all_models = []
        if active:
            model_ports = {}
            for svc in active:
                m = pm.mgr.get_model(svc)
                # Only query vLLM services (not ollama_cpp embedding models)
                if m and m.port and m.type == "vllm":
                    model_ports[svc] = m.port
            if model_ports:
                with ThreadPoolExecutor(max_workers=len(model_ports)) as executor:
                    futures = {executor.submit(_fetch_models, svc, port): svc
                               for svc, port in model_ports.items()}
                    for fut in as_completed(futures):
                        all_models.extend(fut.result())
            # Add ollama/embedding models from config (they don't have /v1/models)
            for svc in active:
                m = pm.mgr.get_model(svc)
                if m and m.type != "vllm":
                    all_models.append({"id": m.served_name or m.name, "object": "model",
                                       "owned_by": "local", "type": m.type})
        else:
            # No active services — return configured model list
            for m in pm.mgr._models.values():
                if m.type != "ollama_daemon":
                    all_models.append({"id": m.served_name or m.name, "object": "model",
                                       "owned_by": "local", "type": m.type})

        # PR-D: Merge cloud models (with capabilities from v4.6.0)
        pm.ensure_cloud_discovered()
        seen_cloud_ids = set()
        for model_id, cm in pm.cloud.cloud_models.items():
            # Skip provider-prefixed keys (e.g., "baidu-codingplan/deepseek-v4-flash")
            # to avoid duplicates — the short name key is already added.
            if "/" in model_id:
                continue
            # Also skip if already in local model list
            existing_ids = {m.get("id") for m in all_models}
            if model_id not in existing_ids and model_id not in seen_cloud_ids:
                seen_cloud_ids.add(model_id)
                all_models.append(cm.to_api_dict())

        if all_models:
            self._send_json({"object": "list", "data": all_models})
        else:
            self._send_json({"error": "no upstream available"}, 503)

    # ─── vLLM Metrics ────────────────────────────────────────────

    def _handle_api_metrics(self, pm):
        """返回聚合指标 (G-2 MetricsAggregator)"""
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            window = qs.get("window", ["24h"])[0]
            if window not in ("1h", "24h", "7d", "all"):
                window = "24h"
            data = pm.metrics.get_metrics(window)
            self._send_json(data, 200)
        except Exception as e:
            log.error("/api/metrics failed: %s", e)
            self._send_json({"error": "metrics unavailable"}, 500)

    def _handle_vllm_metrics(self, pm):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        try:
            port = int(qs.get("port", ["8000"])[0])
        except (ValueError, IndexError):
            self._send_json({"error": "invalid port"}, 400)
            return
        try:
            result, status = handle_vllm_metrics(f"port={port}")
        except RuntimeError as e:
            self._send_json({"error": str(e)}, 502)
            return
        self._send_json(result, status)

    def _handle_request_log(self, pm):
        """返回最近请求日志 (D-1)"""
        try:
            from urllib.parse import urlparse, parse_qs
            import time
            qs = parse_qs(urlparse(self.path).query)
            limit = min(int(qs.get("limit", ["50"])[0]), 500)
            since = float(qs.get("since", ["0"])[0])
            if pm._reqlog_db is None:
                self._send_json({"logs": [], "count": 0}, 200)
                return
            rows = pm._reqlog_db.query_request_log(since=since, limit=limit)
            logs = []
            for r in rows:
                logs.append({
                    "timestamp": r["timestamp"],
                    "model": r["model"],
                    "status": r["status"],
                    "tokens_in": r["tokens_in"],
                    "tokens_out": r["tokens_out"],
                    "ttft_ms": round(r["ttft_ms"], 1) if r["ttft_ms"] else None,
                    "duration_ms": round(r["duration_ms"], 1) if r["duration_ms"] else None,
                    "route": r["route"],
                    "key_name": r.get("key_name", ""),
                    "error": r.get("error", ""),
                })
            self._send_json({"logs": logs, "count": len(logs)}, 200)
        except Exception as e:
            log.error("/api/request_log failed: %s", e)
            self._send_json({"error": "request log unavailable"}, 500)

    # ─── System Info ─────────────────────────────────────────────

    def _system_info(self):
        info = {"cpu_percent": 0, "cpu_cores": os.cpu_count() or 1,
                "ram_total_gb": 0, "ram_used_gb": 0, "uptime_seconds": 0,
                "gpu_util_pct": 0, "gpu_clock_mhz": 0, "gpu_power_w": 0}
        try:
            with open("/proc/meminfo") as f:
                mem = f.read()
            total_kb = int([l for l in mem.splitlines() if l.startswith("MemTotal")][0].split()[1])
            avail_kb = int([l for l in mem.splitlines() if l.startswith("MemAvailable")][0].split()[1])
            info["ram_total_gb"] = round(total_kb / 1024**2, 1)
            info["ram_used_gb"] = round((total_kb - avail_kb) / 1024**2, 1)
        except Exception:
            pass
        try:
            with open("/proc/loadavg") as f:
                loadavg = f.read().split()[0]
            info["cpu_percent"] = round(float(loadavg) / info["cpu_cores"] * 100, 1)
        except Exception:
            pass
        try:
            with open("/proc/uptime") as f:
                info["uptime_seconds"] = int(float(f.read().split()[0]))
        except Exception:
            pass
        try:
            import subprocess as _sub
            r = _sub.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,clocks.current.graphics,power.draw",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0 and r.stdout.strip():
                vals = r.stdout.strip().splitlines()[0].split(",")
                info["gpu_util_pct"] = round(float(vals[0].strip().replace(" ", "")), 1)
                info["gpu_clock_mhz"] = int(vals[1].strip().replace(" ", ""))
                info["gpu_power_w"] = round(float(vals[2].strip().replace(" ", "")), 1)
        except Exception:
            pass
        info["version"] = __version__
        return info

    # ─── Control helpers ─────────────────────────────────────────

    def _read_body(self):
        return forwarder.read_body(self)

    def _send_json(self, data, status=200, extra_headers=None):
        forwarder.send_json(self, data, status, extra_headers=extra_headers)

    # ─── Admin Auth ─────────────────────────────────────────────

    def _check_admin(self) -> bool:
        """Check admin token for control-plane routes.
        Returns True if allowed, False if denied (401 sent)."""
        if not _ADMIN_TOKEN:
            return True  # No token configured → open (localhost-only binding is security)
        token = self.headers.get("X-Admin-Token", "")
        if hmac.compare_digest(token, _ADMIN_TOKEN):
            return True
        self._send_json({"error": "Unauthorized", "status": "unauthorized"}, 401)
        return False

    def _handle_switch(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("model") or data.get("profile")
        if not target:
            self._send_json({"error": "Missing model"}, 400)
            return
        if target == "idle":
            for svc in list(pm.mgr.active_services):
                pm.mgr.state.record_manual_stop(svc)
        elif target != "idle":
            pm.mgr.state.clear_manual_stop(target)
        result = pm.mgr.switch(target)
        self._send_json(result)

    def _handle_stop(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("model")
        if not target:
            self._send_json({"error": "Missing model"}, 400)
            return
        result = pm.mgr.stop_service(target)
        if result.get("status") in ("stopped", "already_stopped"):
            pm.mgr.state.record_manual_stop(target)
        self._send_json(result)

    def _handle_reset(self, pm):
        for svc in list(pm.mgr.active_services):
            pm.mgr.state.record_manual_stop(svc)
        pm.mgr.force_reset()
        self._send_json({"status": "reset", "gpu_mode": GPUMode.IDLE})

    def _handle_sleep(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("model")
        if not target:
            self._send_json({"error": "Missing model"}, 400)
            return
        result = pm.mgr.sleep_model(target)
        self._send_json(result)

    def _handle_wake(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("model")
        if not target:
            self._send_json({"error": "Missing model"}, 400)
            return
        result = pm.mgr.wake_model(target)
        self._send_json(result)

    def _handle_reconcile(self, pm):
        result = pm.mgr.reconcile()
        self._send_json(result)

    def _handle_deploy(self, pm):
        data = self._read_body()
        if data is None:
            return
        name = data.get("name")
        model_type = data.get("type", "vllm")
        if not name:
            self._send_json({"error": "Missing name"}, 400)
            return
        result = pm.mgr.auto_deploy(name, model_type)
        # already_configured means YAML exists; still attempt switch
        if result.get("status") == "already_configured":
            result = pm.mgr.switch(name)
        self._send_json(result)

    def _handle_pull(self, pm):
        data = self._read_body()
        if data is None:
            return
        name = data.get("name")
        framework = data.get("framework", "")
        if not name:
            self._send_json({"error": "Missing name"}, 400)
            return
        result = pm.mgr.pull_model(name, framework)
        self._send_json(result)

    # ─── Admin: Cloud Provider Management (PR-D) ─────────────────

    def _handle_cloud_reload(self, pm):
        """POST /admin/cloud/reload — 热加载 cloud_provider.yaml。"""
        from inferfabric.cloud_discovery import CloudDiscovery
        from inferfabric.proxy_manager import IFF_DATA_DIR
        pm.cloud.reload(IFF_DATA_DIR / "cloud_provider.yaml")
        models = pm.cloud.discover_all()
        pm._cloud_discovered = True
        # Restart polling after reload (reload stops the old polling thread)
        pm.cloud.start_polling()
        self._send_json({
            "status": "reloaded",
            "providers": len(pm.cloud.providers),
            "cloud_models": len(models),
        })

    def _handle_cloud_discover(self, pm):
        """POST /admin/cloud/discover — 手动触发模型发现。"""
        models = pm.cloud.discover_all()
        pm._cloud_discovered = True
        self._send_json({
            "status": "discovered",
            "cloud_models": len(models),
            "models": [
                {
                    "id": m.model_id,
                    "provider": m.provider,
                    "openai": m.openai_available,
                    "anthropic": m.anthropic_available,
                    "discovered_at": m.discovered_at,
                }
                for m in models.values()
            ],
        })

    def _validate_cloud_test_url(self, url: str, pm) -> tuple:
        """Validate a cloud provider test URL for SSRF protection.

        Returns (is_valid: bool, reason: str, resolved_ips: list[str]).
        The resolved_ips list is returned on success so the caller can
        connect directly to the verified IP (TOCTOU mitigation — prevents
        DNS rebinding between validation and connection).
        Checks:
          - scheme must be https
          - host must not resolve to a private/internal IP
          - host must be a registered cloud provider base URL
        """
        # Parse URL
        try:
            parsed = urlparse(url)
        except Exception:
            return False, "invalid URL", []

        # Only allow HTTPS
        if parsed.scheme != "https":
            return False, "only https URLs are allowed", []

        hostname = parsed.hostname
        if not hostname:
            return False, "missing hostname in URL", []

        # DNS resolve and check for private IPs
        _PRIVATE_NETS = [
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("169.254.0.0/16"),
            ipaddress.ip_network("100.64.0.0/10"),  # CGNAT / cloud metadata
            ipaddress.ip_network("::1/128"),
            ipaddress.ip_network("fc00::/7"),
        ]
        safe_ips = []
        try:
            resolved = socket.getaddrinfo(hostname, None)
            for family, _type, _proto, _canonname, sockaddr in resolved:
                ip_str = sockaddr[0]
                try:
                    ip = ipaddress.ip_address(ip_str)
                    # IPv4-mapped IPv6: unwrap and check against v4 nets
                    check_ip = ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip
                    for net in _PRIVATE_NETS:
                        if check_ip in net:
                            return False, f"private IP address not allowed: {ip_str}", []
                    safe_ips.append(ip_str)
                except ValueError:
                    pass
        except socket.gaierror:
            return False, f"DNS resolution failed for {hostname}", []

        if not safe_ips:
            return False, "no valid IPs resolved", []

        # Check against registered cloud provider whitelist
        try:
            pm.ensure_cloud_discovered()
            provider_hosts = set()
            for _pname, pcfg in pm.cloud.providers.items():
                for base_field in ("openai_base", "anthropic_base"):
                    base_url = getattr(pcfg, base_field, "") or ""
                    if base_url:
                        try:
                            parsed_base = urlparse(base_url)
                            if parsed_base.hostname:
                                provider_hosts.add(parsed_base.hostname)
                        except Exception:
                            pass
            if hostname not in provider_hosts:
                return False, f"host '{hostname}' is not a registered cloud provider", []
        except Exception as e:
            log.warning("Cloud provider whitelist check failed: %s", e)
            return False, "cloud provider registry unavailable", []

        return True, "ok", safe_ips

    def _handle_cloud_test(self, pm):
        """POST /admin/cloud/test — 测试 Provider 连接。"""
        data = self._read_body()
        if not data:
            self._send_json({"error": "No body"}, 400)
            return
        url = data.get("url", "")
        api_key = data.get("api_key", "")
        if not url:
            self._send_json({"error": "Missing url"}, 400)
            return

        # SSRF validation (returns resolved IPs to prevent DNS rebinding TOCTOU)
        valid, reason, safe_ips = self._validate_cloud_test_url(url, pm)
        if not valid:
            log.warning("Cloud test URL rejected: %s — reason: %s", url, reason)
            self._send_json({"error": f"SSRF check failed: {reason}"}, 400)
            return

        # Use the first resolved IP directly to prevent DNS rebinding
        # between validation and connection (TOCTOU mitigation).
        # We use http.client.HTTPConnection for the TCP socket to the
        # verified IP, then manually wrap with ssl.SSLSocket using
        # server_hostname=<original hostname> for SNI + cert validation.
        resolved_ip = safe_ips[0]
        try:
            import http.client
            import ssl
            parsed = urlparse(url)
            port = parsed.port or 443
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"

            # Build TCP connection to verified IP, then TLS with original hostname
            ctx = ssl.create_default_context()
            # Step 1: TCP connect to the verified IP (no DNS rebind possible)
            tcp_conn = http.client.HTTPConnection(resolved_ip, port, timeout=15)
            tcp_conn.connect()
            # Step 2: TLS wrap with server_hostname=original hostname (SNI + cert check)
            sock = ctx.wrap_socket(tcp_conn.sock, server_hostname=parsed.hostname)
            tcp_conn.sock = sock
            tcp_conn._http_vsn_str = 'HTTP/1.1'

            headers = {
                "Host": parsed.hostname if not parsed.port else f"{parsed.hostname}:{parsed.port}",
                "Content-Type": "application/json",
            }
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            tcp_conn.request("GET", path, headers=headers)
            resp = tcp_conn.getresponse()
            body = json.loads(resp.read().decode("utf-8")) or {}
            model_count = len(body.get("data", []))
            tcp_conn.close()
            self._send_json({"status": "ok", "model_count": model_count})
        except Exception as e:
            self._send_json({"error": str(e)}, 502)

    def _handle_cloud_providers(self, pm):
        """GET/POST/DELETE /admin/cloud/providers — 列出、添加或删除 provider。"""
        if self.command == "GET":
            providers = []
            for name, cfg in pm.cloud.providers.items():
                env_set = bool(os.environ.get(cfg.key_env_var, "")) if cfg.key_env_var else False
                providers.append({
                    "name": name,
                    "enabled": cfg.enabled,
                    "openai_base": cfg.openai_base,
                    "anthropic_base": cfg.anthropic_base,
                    "discovery_enabled": cfg.discovery_enabled,
                    "discovery_interval": cfg.discovery_interval,
                    "include_pattern": cfg.include_pattern,
                    "model_count": sum(
                        1 for m in pm.cloud.cloud_models.values()
                        if m.provider == name
                    ),
                    "key_env_var": cfg.key_env_var,
                    "key_env_set": env_set,
                    "preset_id": cfg.preset_id,
                })
            # Include cloud models with capabilities (deduplicate: skip provider/ prefixed keys)
            models = []
            seen_ids = set()
            for mid, cm in pm.cloud.cloud_models.items():
                # Dual-key registry: "model_id" + "provider/model_id"
                # Only emit the short-name entry to avoid duplicates
                if "/" in mid:
                    continue
                if cm.model_id in seen_ids:
                    continue
                seen_ids.add(cm.model_id)
                d = cm.to_api_dict()
                d["provider"] = cm.provider
                d["openai_available"] = cm.openai_available
                d["anthropic_available"] = cm.anthropic_available
                d["discovered_at"] = cm.discovered_at
                models.append(d)
            self._send_json({
                "providers": providers,
                "models": models,
                "total_cloud_models": len(pm.cloud.cloud_models),
                "last_discovery": pm.cloud._last_discovery,
            })
        elif self.command == "DELETE":
            data = self._read_body()
            name = (data or {}).get("name", "")
            if name and name in pm.cloud._providers:
                with pm.cloud._models_lock:
                    del pm.cloud._providers[name]
                    pm.cloud._cloud_models = {
                        k: v for k, v in pm.cloud._cloud_models.items()
                        if v.provider != name
                    }
                try:
                    pm.cloud.save_config()
                except Exception as e:
                    log.error("Failed to persist provider deletion: %s", e)
                    self._send_json({"error": "Failed to save config", "detail": str(e)}, 500)
                    return
                self._send_json({"status": "deleted", "provider": name})
            else:
                self._send_json({"error": f"Provider '{name}' not found"}, 404)
        elif self.command == "POST":
            data = self._read_body()
            if data is None:
                return

            # v4.7.0: Support preset-based addition
            preset_id = data.get("preset")
            if preset_id:
                presets = pm.cloud.load_presets()
                preset = presets.get(preset_id)
                if not preset:
                    self._send_json({"error": f"Unknown preset: {preset_id}"}, 400)
                    return
                name = data.get("name", preset_id)
                env_var = preset.env_var or pm.cloud._env_key_for_provider(name)
                # Build model_specs from preset models
                model_specs = {}
                for mid, mspec in preset.models.items():
                    if isinstance(mspec, dict):
                        model_specs[mid] = mspec
            else:
                # Manual mode (backward compatible)
                name = data.get("name")
                if not name:
                    self._send_json({"error": "Missing provider name"}, 400)
                    return
                env_var = pm.cloud._env_key_for_provider(name)

            # #7: Duplicate provider name check
            if name in pm.cloud._providers:
                self._send_json({"error": f"Provider '{name}' already exists"}, 400)
                return

            # #5: Write API key to secrets.env FIRST, before in-memory config
            api_key = data.get("api_key", "")
            if api_key and not api_key.startswith("${"):
                try:
                    pm.cloud.secrets.write(env_var, api_key)
                except Exception as e:
                    log.error("Failed to write secrets.env: %s", e)
                    self._send_json({"error": "Failed to save API key", "detail": str(e)}, 500)
                    return
                api_key_ref = f"${{{env_var}}}"
            else:
                api_key_ref = api_key

            # Inject secrets.env so newly written keys are available immediately
            pm.cloud._inject_secrets_env()

            from inferfabric.cloud_discovery import ProviderConfig
            if preset_id:
                cfg = ProviderConfig(
                    name=name,
                    api_key=api_key_ref,
                    openai_base=preset.openai_base,
                    anthropic_base=preset.anthropic_base,
                    timeout=preset.timeout,
                    enabled=True,
                    discovery_enabled=preset.discovery,
                    key_env_var=env_var,
                    preset_id=preset_id,
                    model_specs=model_specs,
                )
            else:
                cfg = ProviderConfig(
                    name=name,
                    api_key=api_key_ref,
                    openai_base=data.get("openai_base", ""),
                    anthropic_base=data.get("anthropic_base", ""),
                    timeout=data.get("timeout", 60),
                    enabled=data.get("enabled", True),
                    discovery_enabled=data.get("discovery_enabled", True),
                    discovery_endpoint=data.get("discovery_endpoint", "/models"),
                    discovery_interval=data.get("discovery_interval", 3600),
                    include_pattern=data.get("include_pattern", ""),
                    key_env_var=env_var,
                )

            with pm.cloud._models_lock:
                pm.cloud._providers[name] = cfg
                # Register spec-only models from new provider
                pm.cloud._register_spec_only_models(pm.cloud._cloud_models)
            try:
                pm.cloud.save_config()
            except Exception as e:
                log.error("Failed to persist provider addition: %s", e)
                self._send_json({"error": "Failed to save config", "detail": str(e)}, 500)
                return
            self._send_json({"status": "added", "provider": name})

    def _handle_cloud_presets(self, pm):
        """GET /admin/cloud/presets — 返回预设厂商列表。"""
        from inferfabric.cloud_discovery import CloudDiscovery
        presets = CloudDiscovery.load_presets()
        result = []
        for pid, p in presets.items():
            result.append({
                "id": p.id,
                "display_name": p.display_name,
                "icon": p.icon,
                "openai_base": p.openai_base,
                "anthropic_base": p.anthropic_base,
                "env_var": p.env_var,
                "discovery": p.discovery,
                "model_count": len(p.models),
            })
        self._send_json({"presets": result})

    def _handle_embeddings(self, pm):
        """Handle OpenAI-compatible /v1/embeddings requests."""
        data = self._read_body()
        if data is None:
            return

        model_name = data.get("model", "")
        if not model_name:
            self._send_json({"error": "model field is required"}, 400)
            return

        svc_name = pm.model_to_service(model_name)
        if not svc_name:
            self._send_json({"error": f"Unknown model: {model_name}"}, 404)
            return

        model_obj = pm.mgr.get_model(svc_name)
        if not model_obj or model_obj.model_type != "embedding":
            self._send_json({"error": f"Model '{model_name}' is not an embedding model"}, 400)
            return

        port = model_obj.port
        if not port:
            self._send_json({"error": f"No port configured for model '{model_name}'"}, 500)
            return

        # Auto-start if not running
        if svc_name not in pm.mgr.active_services:
            log.info("Embedding model %s not running — auto-starting", svc_name)
            result = pm.mgr.switch(svc_name)
            if result.get("status") != "switched":
                msg = result.get("message", "unknown error")
                log.error("Failed to start embedding model %s: %s", svc_name, msg)
                self._send_json({"error": f"Failed to start embedding model: {msg}"}, 503)
                return
            if not pm._wait_healthy(svc_name, timeout=30):
                self._send_json({"error": f"Embedding model '{svc_name}' failed health check within 30s"}, 503)
                return
        elif not pm._wait_healthy(svc_name, timeout=10):
            log.warning("Embedding model %s not healthy, attempting restart", svc_name)
            pm.mgr.stop_independent(svc_name)
            result = pm.mgr.switch(svc_name)
            if result.get("status") != "switched" or not pm._wait_healthy(svc_name, timeout=30):
                self._send_json({"error": f"Embedding model '{svc_name}' failed to restart"}, 503)
                return

        body = json.dumps(data).encode("utf-8")
        conn = pm.make_conn(port, timeout=30)
        try:
            conn.request("POST", "/v1/embeddings", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp_body = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                self.send_header(k, v)
            self.end_headers()
            self._safe_write(resp_body)
        except Exception as e:
            log.error("Embedding request failed: %s", e)
            self._send_json({"error": "Upstream unavailable", "detail": str(e)}, 503)
        finally:
            conn.close()

    def _handle_rerank(self, pm):
        """Handle /v1/rerank requests — direct port, same pattern as embeddings."""
        data = self._read_body()
        if data is None:
            return

        model_name = data.get("model", "")
        if not model_name:
            self._send_json({"error": "model field is required"}, 400)
            return

        svc_name = pm.model_to_service(model_name)
        if not svc_name:
            self._send_json({"error": f"Unknown model: {model_name}"}, 404)
            return

        model_obj = pm.mgr.get_model(svc_name)
        if not model_obj or model_obj.model_type != "rerank":
            self._send_json({"error": f"Model '{model_name}' is not a rerank model"}, 400)
            return

        port = model_obj.port
        if not port:
            self._send_json({"error": f"No port configured for model '{model_name}'"}, 500)
            return

        # Auto-start if not running
        if svc_name not in pm.mgr.active_services:
            log.info("Rerank model %s not running — auto-starting", svc_name)
            result = pm.mgr.switch(svc_name)
            if result.get("status") != "switched":
                msg = result.get("message", "unknown error")
                log.error("Failed to start rerank model %s: %s", svc_name, msg)
                self._send_json({"error": f"Failed to start rerank model: {msg}"}, 503)
                return
            if not pm._wait_healthy(svc_name, timeout=30):
                self._send_json({"error": f"Rerank model '{svc_name}' failed health check within 30s"}, 503)
                return
        elif not pm._wait_healthy(svc_name, timeout=10):
            log.warning("Rerank model %s not healthy, attempting restart", svc_name)
            pm.mgr.stop_independent(svc_name)
            result = pm.mgr.switch(svc_name)
            if result.get("status") != "switched" or not pm._wait_healthy(svc_name, timeout=30):
                self._send_json({"error": f"Rerank model '{svc_name}' failed to restart"}, 503)
                return

        body = json.dumps(data).encode("utf-8")
        conn = pm.make_conn(port, timeout=30)
        try:
            conn.request("POST", "/v1/rerank", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp_body = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                self.send_header(k, v)
            self.end_headers()
            self._safe_write(resp_body)
        except Exception as e:
            log.error("Rerank request failed: %s", e)
            self._send_json({"error": "Upstream unavailable", "detail": str(e)}, 503)
        finally:
            conn.close()


# ─── Threaded HTTP Server ────────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ─── Main ─────────────────────────────────────────────────────────

def _validate_admin_token_safety():
    """Validate that admin token configuration is safe before starting.

    - If _ADMIN_TOKEN is empty and PROXY_HOST is not localhost → raise RuntimeError
    - If _ADMIN_TOKEN is empty and PROXY_HOST is localhost → warn (acceptable)
    """
    if not _ADMIN_TOKEN:
        if PROXY_HOST in ("127.0.0.1", "localhost", "::1"):
            log.warning(
                "Admin token is empty — control-plane routes are open. "
                "This is acceptable for localhost-only binding but insecure for network access. "
                "Set IFF_ADMIN_TOKEN to a secure value."
            )
        else:
            raise RuntimeError(
                f"Admin token is empty but PROXY_HOST={PROXY_HOST!r} is not localhost. "
                "This would expose control-plane routes without authentication. "
                "Set IFF_ADMIN_TOKEN to a secure value or set PROXY_HOST to 127.0.0.1."
            )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    log_dir = Path.home() / ".inferfabric" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(log_dir / "proxy.log", maxBytes=10_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
    logging.getLogger("inferfabric").addHandler(fh)

    _validate_admin_token_safety()

    mgr = ProxyManager()
    shutdown_event = threading.Event()

    # Start runtime health watchdog
    watchdog = ModelWatchdog(mgr.mgr, check_interval=30, auto_restart=True)
    watchdog.start()

    server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
    server.proxy_mgr = mgr
    server.watchdog = watchdog

    _notify_socket = os.environ.get('NOTIFY_SOCKET')
    _notify_enabled = bool(_notify_socket)

    def sd_notify(message: str):
        if not _notify_socket:
            return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.connect(_notify_socket)
            sock.sendall(message.encode())
            sock.close()
        except Exception:
            pass

    def handle_signal(signum, frame):
        log.info("Received signal %s, initiating shutdown", signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    def watchdog_loop():
        while not shutdown_event.is_set():
            shutdown_event.wait(WATCHDOG_INTERVAL)
            if not shutdown_event.is_set():
                sd_notify("WATCHDOG=1")

    if _notify_enabled:
        threading.Thread(target=watchdog_loop, daemon=True, name="watchdog").start()

    def health_loop():
        while not shutdown_event.is_set():
            shutdown_event.wait(HEALTH_CHECK_INTERVAL)
            if not shutdown_event.is_set():
                mgr.health_check()

    try:
        rec = mgr.mgr.reconcile()
        if rec.get("actions"):
            log.info("Startup reconcile: %s", rec["actions"])
    except Exception as e:
        log.warning("Startup reconcile failed: %s", e)

    threading.Thread(target=health_loop, daemon=True, name="health").start()

    # Start token stats collector (5 min interval)
    token_collector = TokenStatsCollector(manager_ref=lambda: mgr.mgr, interval=300)
    token_collector.start()

    sd_notify("READY=1")
    log.info("InferFabric Proxy: %s:%d (auto_switch=%s, threaded, v%s)",
             PROXY_HOST, PROXY_PORT, AUTO_SWITCH, __version__)
    log.info("Dashboard: http://%s:%d/", PROXY_HOST, PROXY_PORT)
    log.info("GPU mode: %s | Services: %s", mgr.mgr.gpu_mode, mgr.mgr.active_services)

    try:
        while not shutdown_event.is_set():
            # handle_request() blocks indefinitely when no connections arrive,
            # preventing shutdown_event from being checked.
            # Use select with timeout to make the loop responsive to SIGTERM.
            import select as _select
            readable, _, _ = _select.select([server.socket], [], [], 1.0)
            if readable:
                server.handle_request()
            # If no readable sockets, loop back and check shutdown_event
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Closing server...")
        watchdog.stop()
        try:
            server.server_close()
        except Exception:
            pass
        log.info("Shutdown complete")
        sd_notify("STOPPING=1")
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()
