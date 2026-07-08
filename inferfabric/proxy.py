"""
inferfabric/proxy.py — Auto-routing proxy + web dashboard.

v4.0: Model-plugin architecture. Forwarding logic extracted to forwarder.py.
"""

import sys
import os
import signal
import socket
import logging
import json
import http.server
import socketserver
import threading
import time
from pathlib import Path
from http.client import HTTPConnection
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent))
from inferfabric.manager import ModelManager
from inferfabric.state import GPUMode, ProfileState
from inferfabric.config import load_aliases

log = logging.getLogger("inferfabric.proxy")


# ─── Config ──────────────────────────────────────────────────────

PROXY_HOST = os.environ.get("EDGE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("EDGE_PROXY_PORT", "8999"))
AUTO_SWITCH = os.environ.get("EDGE_AUTO_SWITCH", "1") == "1"
HEALTH_CHECK_INTERVAL = int(os.environ.get("EDGE_HEALTH_CHECK", "60"))
WATCHDOG_INTERVAL = 20


# ─── Proxy Manager ──────────────────────────────────────────────

class ProxyManager:
    """Manages model switching + request routing (v4.0: model-plugin)."""

    def __init__(self):
        self.mgr = ModelManager()
        self._aliases = load_aliases()
        self._last_switch = 0.0
        self._cooldown = 10
        self._switch_lock = threading.Lock()
        log.info("Loaded %d model aliases: %s", len(self._aliases), list(self._aliases.keys()))

    @property
    def current(self) -> str:
        """Current active service or 'idle'."""
        return self.mgr.current_service

    def model_to_service(self, model_name: str):
        """Map served_model_name to model config name. Resolves aliases first."""
        resolved = self._aliases.get(model_name, model_name)
        m = self.mgr.find_model_by_served_name(resolved)
        if m:
            log.debug("model_to_service: %s → %s (served=%s)", model_name, resolved, m.name)
            return m.name
        if resolved != model_name:
            m2 = self.mgr.find_model_by_served_name(model_name)
            if m2:
                return m2.name
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
        resolved = self._aliases.get(model_name, model_name)
        m = self.mgr.find_model_by_served_name(resolved)
        if not m and resolved != model_name:
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
            stops = json.loads(self.mgr.state.get("manual_stops") or "{}")
            expired = [k for k, v in stops.items() if time.time() - v > self.mgr.state.MANUAL_STOP_TTL]
            if expired:
                for k in expired:
                    del stops[k]
                self.mgr.state.set("manual_stops", json.dumps(stops))
                log.debug("Cleaned %d expired manual_stop records", len(expired))
        except Exception as e:
            log.debug("Manual stop cleanup error: %s", e)


# ─── HTTP Handler ───────────────────────────────────────────────

from inferfabric import forwarder


class _RateLimiter:
    """Per-model vLLM concurrency gate.

    Semaphore caps concurrent requests to max_num_seqs.
    Requests failing to acquire within timeout → 429 + Retry-After.

    Only applies to local vLLM forwarding; Baidu/Ollama bypass.
    """

    def __init__(self, max_concurrent: int = 6, timeout: float = 30.0):
        self._sem = threading.Semaphore(max_concurrent)
        self._timeout = timeout
        self._max_concurrent = max_concurrent

    def acquire(self) -> bool:
        return self._sem.acquire(timeout=self._timeout)

    def release(self):
        self._sem.release()


# P0: matches qwen36-27b max_num_seqs=8
# TODO: dynamically read from model YAML; Baidu/Ollama excluded
_VLLM_RATE_LIMITER = _RateLimiter(max_concurrent=6, timeout=30.0)
_MODEL_RATE_LIMITERS: dict[str, _RateLimiter] = {}
_MODEL_LIMITER_MAX = 50

def _get_model_rate_limiter(pm, model_name: str) -> _RateLimiter:
    """Get per-model rate limiter based on YAML max_num_seqs."""
    if model_name in _MODEL_RATE_LIMITERS:
        return _MODEL_RATE_LIMITERS[model_name]
    # Guard against unbounded growth
    if len(_MODEL_RATE_LIMITERS) >= _MODEL_LIMITER_MAX:
        _MODEL_RATE_LIMITERS.clear()
    try:
        model_obj = pm.mgr.get_model(model_name)
        if model_obj and model_obj.config and model_obj.config.max_num_seqs:
            max_concurrent = model_obj.config.max_num_seqs
        else:
            max_concurrent = 6
    except Exception:
        max_concurrent = 6
    # Enforce bounds: [2, 20] — Semaphore(0) would block forever; cap at 20
    max_concurrent = max(2, min(max_concurrent, 20))
    limiter = _RateLimiter(max_concurrent=max_concurrent, timeout=30.0)
    _MODEL_RATE_LIMITERS[model_name] = limiter
    return limiter


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    # Per-port state for counter-diff throughput (MTP-aware)
    _vllm_gen_counters: dict = {}  # port -> (timestamp, generation_tokens_total)
    # EMA state for smoothed throughput: port -> ema_value (tokens/s)
    _vllm_throughput_ema: dict = {}  # port -> float
    _vllm_metrics_lock = threading.Lock()  # protects _vllm_gen_counters + _vllm_throughput_ema

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
            elif self.path in ("/models", "/profiles"):
                self._send_json(pm.mgr.list_models())
            elif self.path == "/local-models":
                self._send_json(pm.mgr.discover_local_models())
            elif self.path == "/v1/models":
                self._handle_v1_models(pm)
            elif self.path == "/system":
                self._send_json(self._system_info())
            elif self.path == "/history":
                self._send_json(pm.mgr.state.get_history(limit=30))
            elif path == "/vllm_metrics":
                self._handle_vllm_metrics(pm)
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
                self._handle_switch(pm)
            elif path == "/stop":
                self._handle_stop(pm)
            elif path == "/sleep":
                self._handle_sleep(pm)
            elif path == "/wake":
                self._handle_wake(pm)
            elif path in ("/api/chat", "/api/generate"):
                self._handle_chat(pm)
            elif path == "/reset":
                self._handle_reset(pm)
            elif path == "/reconcile":
                self._handle_reconcile(pm)
            elif path == "/deploy":
                self._handle_deploy(pm)
            elif path == "/v1/embeddings":
                self._handle_embeddings(pm)
            else:
                self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.error("POST %s error: %s", self.path, e)

    def _serve_dashboard(self):
        body = None
        try:
            from inferfabric.dashboard import DASHBOARD_HTML
            body = DASHBOARD_HTML.encode("utf-8")
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
        self.end_headers()
        self._safe_write(body)

    # ── Anthropic Messages handler (/v1/messages) ──

    def _handle_messages(self, pm):
        """Handle Anthropic Messages API requests.

        Fallback chain:
          1. Active local LLM/VL model(s) — dynamic lookup
          2. Baidu Coding Plan — cloud fallback
          3. 503 if both fail
        """
        data = self._read_body()
        if data is None:
            return

        original_model = data.get("model", "")
        auth_header = self.headers.get("Authorization", "") or self.headers.get("x-api-key", "")

        log.info("/v1/messages body: max_tokens=%s, model=%s, messages_count=%d, tools_count=%d, body_size=%d",
                 data.get("max_tokens"), data.get("model"),
                 len(data.get("messages", [])),
                 len(data.get("tools", [])),
                 len(json.dumps(data)))

        # Find active local LLM/VL services
        active_llm = None
        for svc in pm.mgr.active_services:
            model_obj = pm.mgr.get_model(svc)
            if model_obj and model_obj.model_type in forwarder.LOCAL_LLM_TYPES:
                if model_obj.port:
                    active_llm = model_obj
                    break

        if active_llm:
            log.info("/v1/messages → LOCAL %s (port %d)", active_llm.name, active_llm.port)
            if not _VLLM_RATE_LIMITER.acquire():
                self._send_json(
                    {"error": "vLLM at capacity, try again later", "status": "rate_limit"},
                    429,
                )
                return
            try:
                forwarder.forward_anthropic_local(
                    self, pm, data, auth_header, active_llm, original_model
                )
            finally:
                _VLLM_RATE_LIMITER.release()
        else:
            log.info("/v1/messages → BAIDU fallback")
            forwarder.forward_to_baidu(self, data, auth_header, original_model)

    # ── OpenAI chat handler ──

    def _forward_request(self, pm, target_port, body, stream):
        """Forward a request to an upstream service.

        Returns True if the response was fully sent to the client.
        Returns False if the caller should retry (headers not yet sent).
        """
        headers_sent = False
        conn = None
        resp = None
        try:
            conn = pm.make_conn(target_port)
            conn.request("POST", self.path, body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()

            resp_status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            resp_ct = resp_headers.get("content-type", "application/json")

            if stream:
                # Streaming: forward chunk-by-chunk
                headers_sent = True
                self.send_response(resp_status)
                self.send_header("Content-Type", resp_ct)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        size = f"{len(chunk):x}\r\n".encode()
                        self._safe_write(size)
                        self._safe_write(chunk)
                        self._safe_write(b"\r\n")
                    self._safe_write(b"0\r\n\r\n")
                except Exception as e:
                    log.debug("Stream forwarding interrupted: %s", e)
                finally:
                    resp.close()
            else:
                # Non-streaming: buffer then send
                try:
                    resp_body = resp.read()
                finally:
                    resp.close()
                headers_sent = True
                self.send_response(resp_status)
                self.send_header("Content-Type", resp_ct)
                self.send_header("Content-Length", str(len(resp_body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self._safe_write(resp_body)
            return True
        except Exception as e:
            log.error("Forward to :%d failed: %s", target_port, e)
            try:
                if resp:
                    resp.close()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            if not headers_sent:
                return False
            return True

    def _handle_chat(self, pm):
        data = self._read_body()
        if data is None:
            return

        model = data.get("model", "vllm_qwen27b")
        stream = data.get("stream", False)

        # Enable Qwen3 thinking/reasoning mode by default (vLLM only)
        svc_name = pm.model_to_service(model)
        model_obj = pm.mgr.get_model(svc_name) if svc_name else None
        if model_obj and model_obj.is_vllm:
            if "chat_template_kwargs" not in data:
                data["chat_template_kwargs"] = {}
            if "enable_thinking" not in data.get("chat_template_kwargs", {}):
                data["chat_template_kwargs"]["enable_thinking"] = True

        # Dynamic model lookup
        service_name = pm.model_to_service(model)
        if service_name and AUTO_SWITCH:
            switched = pm.ensure_service(service_name)
            if switched is None:
                self._send_json({"error": "switch already in progress", "status": "conflict"}, 409)
                return
            if not switched and service_name not in pm.mgr.active_services:
                if pm.mgr.state.is_manually_stopped(service_name):
                    reason = f"{service_name} was manually stopped — auto-switch blocked for {pm.mgr.state.MANUAL_STOP_TTL}s"
                else:
                    reason = "tri-state rule violation or switch in progress"
                self._send_json({"error": f"Cannot switch to {reason}"}, 503)
                return

        target_port = pm.get_target_port(model)
        if not target_port:
            self._send_json({"error": f"Unknown model: {model}"}, 404)
            return

        # Rewrite model name to upstream's served_name
        ollama_model_obj = None
        if service_name:
            model_obj = pm.mgr.get_model(service_name)
            if model_obj and model_obj.served_name:
                data["model"] = model_obj.served_name
            if model_obj and (model_obj.is_ollama or model_obj.is_ollama_cpp):
                ollama_model_obj = model_obj

        if ollama_model_obj and ollama_model_obj.is_ollama and ollama_model_obj.ollama and ollama_model_obj.ollama.num_gpu >= 0:
            self._handle_chat_ollama_native(pm, data, target_port, stream, ollama_model_obj)
            return
        if ollama_model_obj and ollama_model_obj.is_ollama_cpp:
            self._handle_chat_ollama_native(pm, data, target_port, stream, ollama_model_obj)
            return

        # vLLM path — apply dynamic rate limiter
        body = json.dumps(data).encode("utf-8")
        limiter = _get_model_rate_limiter(pm, model)
        if not limiter.acquire():
            self._send_json(
                {"error": "vLLM at capacity, try again later", "status": "rate_limit"},
                429,
            )
            return
        try:
            for attempt in range(2):
                if self._forward_request(pm, target_port, body, stream):
                    return
                if attempt == 0:
                    time.sleep(0.5)
            self._send_json({"error": "Upstream unavailable after retry"}, 502)
        finally:
            limiter.release()

    def _handle_chat_ollama_native(self, pm, data, target_port, stream, model_obj):
        """Handle chat for Ollama backends using native /api/chat API."""
        ollama_req = {
            "model": data["model"],
            "messages": data.get("messages", []),
            "stream": stream,
            "options": {},
        }
        if model_obj.ollama and model_obj.ollama.num_gpu >= 0:
            ollama_req["options"]["num_gpu"] = model_obj.ollama.num_gpu
        if data.get("max_tokens"):
            ollama_req["options"]["num_predict"] = data["max_tokens"]
        if model_obj.ollama and model_obj.ollama.keep_alive:
            ollama_req["keep_alive"] = model_obj.ollama.keep_alive
        if model_obj.ollama_cpp and model_obj.ollama_cpp.gpu_layers != 0:
            ollama_req["options"]["num_gpu"] = model_obj.ollama_cpp.gpu_layers

        body = json.dumps(ollama_req).encode("utf-8")

        conn = None
        try:
            conn = pm.make_conn(target_port)
            conn.request("POST", "/api/chat", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
        except Exception as e:
            log.error("Ollama native forward to :%d failed: %s", target_port, e)
            self._send_json({"error": str(e)}, 502)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            return

        try:
            resp_status = resp.status
            if resp_status != 200:
                err_body = resp.read().decode("utf-8", errors="replace")
                self._send_json({"error": f"Ollama error: {err_body[:500]}"}, resp_status)
                return

            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                import uuid
                chat_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
                full_content = ""
                try:
                    buffer = b""
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                                content = obj.get("message", {}).get("content", "")
                                if content:
                                    full_content += content
                                    sse_data = json.dumps({
                                        "id": chat_id,
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": data["model"],
                                        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
                                    })
                                    self._safe_write(f"data: {sse_data}\n\n".encode())
                            except json.JSONDecodeError:
                                pass
                    sse_done = json.dumps({
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": data["model"],
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                    })
                    self._safe_write(f"data: {sse_done}\n\n".encode())
                    self._safe_write(b"data: [DONE]\n\n")
                except Exception as e:
                    log.error("Ollama native stream error: %s", e)
            else:
                resp_body = resp.read()
                try:
                    obj = json.loads(resp_body)
                    full_content = obj.get("message", {}).get("content", "")
                    total_input = obj.get("prompt_eval_count", 0) or 0
                    total_output = obj.get("eval_count", 0) or 0
                except json.JSONDecodeError:
                    full_content = resp_body.decode("utf-8", errors="replace")
                    total_input = 0
                    total_output = 0
                import uuid
                self._send_json({
                    "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": data["model"],
                    "system_fingerprint": "fp_ollama",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": full_content},
                        "finish_reason": "stop"
                    }],
                    "usage": {
                        "prompt_tokens": total_input,
                        "completion_tokens": total_output,
                        "total_tokens": total_input + total_output
                    }
                })
        except Exception as e:
            log.error("Ollama native response error: %s", e)
            self._send_json({"error": str(e)}, 500)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            try:
                resp.close()
            except Exception:
                pass

    def _handle_v1_models(self, pm):
        """Forward /v1/models to active upstream services."""
        active = list(pm.mgr.active_services)
        if not active:
            models_d = pm.mgr.list_models()
            self._send_json(models_d)
            return

        # Forward /v1/models to active upstream services (concurrent)

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

        # Pre-fetch model→port mapping (avoid repeated get_model calls)
        model_ports = {}
        for svc in active:
            m = pm.mgr.get_model(svc)
            if m and m.port:
                model_ports[svc] = m.port

        all_models = []
        if model_ports:
            with ThreadPoolExecutor(max_workers=len(model_ports)) as executor:
                futures = {executor.submit(_fetch_models, svc, port): svc
                           for svc, port in model_ports.items()}
                for fut in as_completed(futures):
                    all_models.extend(fut.result())

        all_models.append({"id": "qianfan-code-latest", "object": "model", "owned_by": "proxy", "permission": []})

        if all_models:
            self._send_json({"object": "list", "data": all_models})
        else:
            self._send_json({"error": "no upstream available"}, 503)

    def _handle_vllm_metrics(self, pm):
        """Fetch and parse vLLM Prometheus /metrics endpoint."""
        import urllib.request
        import math
        from urllib.parse import urlparse, parse_qs

        qs = parse_qs(urlparse(self.path).query)
        try:
            port = int(qs.get("port", ["8000"])[0])
        except (ValueError, IndexError):
            self._send_json({"error": "invalid port"}, 400)
            return

        url = f"http://127.0.0.1:{port}/metrics"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                text = resp.read().decode("utf-8")
        except Exception as e:
            self._send_json({"error": str(e)}, 502)
            return

        gauges, counters, histos = {}, {}, {}

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            bracket = line.find("{")
            if bracket >= 0:
                name = line[:bracket]
                close = line.rfind("}")
                val_str = line[close+1:].strip() if close > bracket else ""
            else:
                parts = line.split()
                if len(parts) < 2:
                    continue
                name, val_str = parts[0], parts[1]

            try:
                val = float(val_str)
            except ValueError:
                continue

            if name.endswith("_bucket"):
                base = name[:-7]
                le_start = line.find('le="', bracket) if bracket >= 0 else -1
                le_end = line.find('"', le_start + 4) if le_start >= 0 else -1
                le_val = float(line[le_start+4:le_end]) if le_start >= 0 else math.inf
                if base not in histos:
                    histos[base] = {"buckets": [], "sum": 0.0, "count": 0}
                histos[base]["buckets"].append((le_val, int(val)))
            elif name.endswith("_sum"):
                base = name[:-4]
                if base not in histos:
                    histos[base] = {"buckets": [], "sum": 0.0, "count": 0}
                histos[base]["sum"] = val
            elif name.endswith("_count"):
                base = name[:-6]
                if base not in histos:
                    histos[base] = {"buckets": [], "sum": 0.0, "count": 0}
                histos[base]["count"] = int(val)
            elif "_total" in name:
                counters[name.rsplit("_total", 1)[0]] = val
            else:
                gauges[name] = val

        def _quantile(buckets, count, q):
            if count == 0 or not buckets:
                return None
            target = count * q
            sorted_bk = sorted(buckets, key=lambda x: x[0])
            cum = 0
            for i, (le, c) in enumerate(sorted_bk):
                cum = c
                if cum >= target:
                    if i == 0:
                        return le / 2
                    prev_le, prev_c = sorted_bk[i - 1]
                    if math.isfinite(le) and c > prev_c:
                        return prev_le + (le - prev_le) * (target - prev_c) / (c - prev_c)
                    return prev_le
            return sorted_bk[-1][0]

        result = {}

        kv = gauges.get("vllm:kv_cache_usage_perc")
        if kv is not None:
            result["kv_cache_usage_perc"] = round(kv * 100, 1)

        ttft = histos.get("vllm:time_to_first_token_seconds")
        if ttft and ttft["count"] > 0:
            result["ttft_seconds"] = {
                "p50": round(_quantile(ttft["buckets"], ttft["count"], 0.50), 3),
                "p95": round(_quantile(ttft["buckets"], ttft["count"], 0.95), 3),
                "mean": round(ttft["sum"] / ttft["count"], 3),
                "count": ttft["count"],
            }
            result["ttft_cum_mean"] = round(ttft["sum"] / ttft["count"], 3)
            result["ttft_cum_n"] = ttft["count"]

        tpot = histos.get("vllm:request_time_per_output_token_seconds")
        if tpot and tpot["count"] > 0:
            result["tpot_seconds"] = {
                "p50": round(_quantile(tpot["buckets"], tpot["count"], 0.50), 3),
                "p95": round(_quantile(tpot["buckets"], tpot["count"], 0.95), 3),
                "mean": round(tpot["sum"] / tpot["count"], 4),
                "count": tpot["count"],
            }
            result["tpot_cum_mean"] = round(tpot["sum"] / tpot["count"], 4)
            result["tpot_cum_n"] = tpot["count"]

        prompt_h = histos.get("vllm:request_prompt_tokens")
        gen_h = histos.get("vllm:request_generation_tokens")
        total_reqs = 0
        if prompt_h:
            total_reqs = prompt_h.get("count", 0)
        if prompt_h and gen_h and total_reqs > 0:
            avg_prompt = round(prompt_h["sum"] / total_reqs)
            avg_gen = round(gen_h["sum"] / total_reqs)
            result["seq_length"] = avg_prompt + avg_gen
            result["seq_prompt"] = avg_prompt
            result["seq_generation"] = avg_gen
            result["seq_count"] = total_reqs

        # Throughput: EMA-based (MTP-aware). Industry standard = total_output_tokens / elapsed_time.
        # Idle time must not dilute throughput — use EMA over active 10s windows.
        # alpha=0.3 → ~3-4 samples determine average (30-40s window), responsive but stable.
        # When idle (actual_tokens == 0), EMA is NOT updated — last active value persists.
        EMA_ALPHA = 0.3
        gen_key = "vllm:generation_tokens"
        gen_counter = counters.get(gen_key)
        cur_ts = time.time()

        # Single-lock section: all reads/writes to shared dicts in one critical section
        with self._vllm_metrics_lock:
            prev_state = self._vllm_gen_counters.get(port)

            if gen_counter is not None:
                inst_tp = None
                if prev_state is not None:
                    prev_ts, prev_val = prev_state
                    elapsed = cur_ts - prev_ts
                    actual_tokens = int(gen_counter) - int(prev_val)
                    if elapsed > 0 and actual_tokens > 0:
                        inst_tp = round(actual_tokens / elapsed, 1)

                # EMA update: only when actual generation happened
                prev_ema = self._vllm_throughput_ema.get(port)
                if inst_tp is not None:
                    if prev_ema is None:
                        ema_tp = inst_tp
                    else:
                        ema_tp = EMA_ALPHA * inst_tp + (1 - EMA_ALPHA) * prev_ema
                    self._vllm_throughput_ema[port] = ema_tp
                    result["throughput"] = round(ema_tp, 1)
                    result["throughput_inst"] = inst_tp
                    result["throughput_cum_n"] = int(gen_counter)
                elif prev_ema is not None:
                    # Idle: keep last EMA value
                    result["throughput"] = round(prev_ema, 1)
                    result["throughput_cum_n"] = int(gen_counter)

            self._vllm_gen_counters[port] = (cur_ts, gen_counter)

        self._send_json(result)

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
        return info

    def _read_body(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                return {}
            if content_length > 10 * 1024 * 1024:
                self._send_json({"error": "payload too large (max 10MB)"}, 413)
                return None
            body = self.rfile.read(content_length)
            return json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json({"error": f"Invalid JSON: {e}"}, 400)
            return None

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
        self._send_json(result)

    def _handle_embeddings(self, pm):
        """Handle OpenAI-compatible /v1/embeddings requests.

        Looks up the embedding model by served_name, starts it if not running,
        and forwards the request to the upstream service.

        Timeout: 30s per phase (health check, forward request). Worst case ~60s total.
        """
        data = self._read_body()
        if data is None:
            return

        model_name = data.get("model", "")
        if not model_name:
            self._send_json({"error": "model field is required"}, 400)
            return

        # Resolve served model name → service (config) name
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

        # Auto-start if not running (embedding models are GPU-independent)
        if svc_name not in pm.mgr.active_services:
            log.info("Embedding model %s not running — auto-starting", svc_name)
            result = pm.mgr.switch(svc_name)
            if result.get("status") != "switched":
                msg = result.get("message", "unknown error")
                log.error("Failed to start embedding model %s: %s", svc_name, msg)
                self._send_json({"error": f"Failed to start embedding model: {msg}"}, 503)
                return
            # Wait for healthy (30s timeout)
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

        # Forward the request to upstream /v1/embeddings
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

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self._safe_write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# ─── Threaded HTTP Server ───────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ─── Main ─────────────────────────────────────────────────────────

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

    mgr = ProxyManager()
    shutdown_event = threading.Event()

    server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
    server.proxy_mgr = mgr

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

    sd_notify("READY=1")
    log.info("InferFabric Proxy: %s:%d (auto_switch=%s, threaded, v4.0)",
             PROXY_HOST, PROXY_PORT, AUTO_SWITCH)
    log.info("Dashboard: http://%s:%d/", PROXY_HOST, PROXY_PORT)
    log.info("GPU mode: %s | Services: %s", mgr.mgr.gpu_mode, mgr.mgr.active_services)

    try:
        while not shutdown_event.is_set():
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Closing server...")
        try:
            server.server_close()
        except Exception:
            pass
        log.info("Shutdown complete")
        sd_notify("STOPPING=1")
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()
