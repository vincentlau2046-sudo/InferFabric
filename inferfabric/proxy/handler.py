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
import http.server
import socketserver
import threading
import time
from pathlib import Path
from http.client import HTTPConnection
from concurrent.futures import ThreadPoolExecutor, as_completed

from inferfabric.state import GPUMode
from inferfabric.proxy_manager import (
    ProxyManager, AUTO_SWITCH, PROXY_HOST, PROXY_PORT,
    HEALTH_CHECK_INTERVAL, WATCHDOG_INTERVAL,
)
from inferfabric import forwarder
from inferfabric.proxy.chat_handlers import handle_chat, handle_ollama_native
from inferfabric.proxy.metrics import handle_vllm_metrics

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

    # ─── Dashboard ────────────────────────────────────────────────

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

    # ─── Chat ─────────────────────────────────────────────────────

    def _handle_chat(self, pm):
        data = self._read_body()
        if data is None:
            return
        handle_chat(self, pm, data)

    # ─── Anthropic Messages handler ───────────────────────────────

    def _handle_messages(self, pm):
        """Handle Anthropic Messages API requests."""
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

        active_llm = None
        for svc in pm.mgr.active_services:
            model_obj = pm.mgr.get_model(svc)
            if model_obj and model_obj.model_type in forwarder.LOCAL_LLM_TYPES:
                if model_obj.port:
                    active_llm = model_obj
                    break

        if active_llm:
            log.info("/v1/messages → LOCAL %s (port %d)", active_llm.name, active_llm.port)
            model_name = data.get("model", "vllm_qwen27b")
            from inferfabric.ratelimit import _get_model_rate_limiter
            limiter = _get_model_rate_limiter(pm, model_name)
            if not limiter.acquire():
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
                limiter.release()
        else:
            log.info("/v1/messages → BAIDU fallback")
            forwarder.forward_to_baidu(self, data, auth_header, original_model)

    # ─── v1 Models ────────────────────────────────────────────────

    def _handle_v1_models(self, pm):
        """Forward /v1/models to active upstream services."""
        active = list(pm.mgr.active_services)
        if not active:
            models_d = pm.mgr.list_models()
            self._send_json(models_d)
            return

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

    # ─── vLLM Metrics ────────────────────────────────────────────

    def _handle_vllm_metrics(self, pm):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        try:
            port = int(qs.get("port", ["8000"])[0])
        except (ValueError, IndexError):
            self._send_json({"error": "invalid port"}, 400)
            return
        try:
            result = handle_vllm_metrics(port)
        except RuntimeError as e:
            self._send_json({"error": str(e)}, 502)
            return
        self._send_json(result)

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
        return info

    # ─── Control helpers ─────────────────────────────────────────

    def _read_body(self):
        return forwarder.read_body(self)

    def _send_json(self, data, status=200):
        forwarder.send_json(self, data, status)

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


# ─── Threaded HTTP Server ────────────────────────────────────────

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