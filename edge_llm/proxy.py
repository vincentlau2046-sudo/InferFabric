"""
edge_llm/proxy.py — Robust auto-routing proxy + web dashboard.

v3.2 rewrite — fixes:
  - shutdown deadlock: handle_request() loop + Event flag (no server.shutdown())
  - BrokenPipeError: silently caught in all response paths
  - streaming proxy: robust chunk forwarding with error recovery
  - systemd watchdog: independent thread, decoupled from health_check
  - connection reuse: HTTPConnection per upstream, kept alive

Architecture:
  Main thread: handle_request() loop with select timeout
  Signal handler: sets shutdown Event (thread-safe)
  Watchdog thread: sd_notify(WATCHDOG=1) every 20s
  Health thread: status logging every N seconds
  Worker threads: per-connection via ThreadingMixIn
"""

import sys
import os
import signal
import socket
import select
import logging
import json
import http.server
import socketserver
import threading
import time
from pathlib import Path
from http.client import HTTPConnection

sys.path.insert(0, str(Path(__file__).parent.parent))
from edge_llm.manager import ProfileManager
from edge_llm.state import ProfileState

log = logging.getLogger("edge_llm.proxy")

# ─── Config ──────────────────────────────────────────────────────

PROXY_HOST = os.environ.get("EDGE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("EDGE_PROXY_PORT", "8999"))
AUTO_SWITCH = os.environ.get("EDGE_AUTO_SWITCH", "1") == "1"
HEALTH_CHECK_INTERVAL = int(os.environ.get("EDGE_HEALTH_CHECK", "60"))
WATCHDOG_INTERVAL = 20  # seconds between sd_notify WATCHDOG=1


# ─── Proxy Manager ──────────────────────────────────────────────

class ProxyManager:
    """Manages profile switching + request routing."""

    def __init__(self):
        self.mgr = ProfileManager()
        self._last_switch = 0.0
        self._cooldown = 10
        self._switch_lock = threading.Lock()
        self._upstream_pool: dict[int, HTTPConnection] = {}

    @property
    def current(self) -> str:
        return self.mgr.current_profile

    def model_to_profile(self, model_name: str):
        model_map = {
            "vllm_qwen27b": "qw36_full",
            "vllm_qw35_gptq": "qw35_comfyui",
            "vllm_gemma26b_nvfp4": "gemma_full",
        }
        return model_map.get(model_name)

    def ensure_profile(self, target: str) -> bool:
        if self.current == target:
            return True
        if not self._switch_lock.acquire(blocking=False):
            log.warning("Switch already in progress, skipping")
            return False
        try:
            if time.time() - self._last_switch < self._cooldown:
                log.warning("Switch cooldown active, skipping")
                return False
            log.info("Auto-switch: %s → %s", self.current, target)
            result = self.mgr.switch(target)
            self._last_switch = time.time()
            return result["status"] == "switched"
        finally:
            self._switch_lock.release()

    def get_target_port(self, model_name: str):
        for name, p in self.mgr._profiles.items():
            if p.vllm and p.vllm.served_name == model_name:
                return p.vllm.port
        return None

    def get_upstream(self, port: int) -> HTTPConnection:
        """Get or create a keep-alive HTTPConnection to upstream."""
        conn = self._upstream_pool.get(port)
        if conn:
            try:
                # Test if connection is still usable
                conn.request("GET", "/health")
                resp = conn.getresponse()
                resp.read()  # drain
                if resp.status in (200, 503):
                    return conn
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

        conn = HTTPConnection("127.0.0.1", port, timeout=300)
        self._upstream_pool[port] = conn
        return conn

    def health_check(self):
        try:
            s = self.mgr.status()
            log.info("Health check: profile=%s state=%s vllm=%s comfyui=%s",
                     s["profile"], s.get("state", "?"), s["vllm"], s["comfyui"])
            p = self.mgr._profiles.get(s["profile"])
            if p and p.vllm and s["vllm"] == "❌" and s.get("state") == ProfileState.HEALTHY:
                log.warning("vLLM unhealthy but state=healthy — use `edge-llm reconcile` to fix")
            if p and p.comfyui and s["comfyui"] == "❌" and s.get("state") == ProfileState.HEALTHY:
                log.warning("ComfyUI unhealthy but state=healthy — use `edge-llm reconcile` to fix")
        except Exception as e:
            log.error("Health check exception: %s", e)


# ─── HTTP Handler ───────────────────────────────────────────────

class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug("[proxy] " + fmt, *args)

    @property
    def proxy(self):
        return self.server.proxy_mgr

    def _safe_write(self, data: bytes):
        """Write to wfile, silently catching BrokenPipeError."""
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except BrokenPipeError:
            log.debug("Client disconnected (BrokenPipe)")
        except ConnectionResetError:
            log.debug("Client disconnected (ConnectionReset)")
        except OSError:
            pass

    def _safe_close(self):
        """End headers and flush, silently catching errors."""
        try:
            self.wfile.flush()
        except Exception:
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
            if self.path == "/":
                self._serve_dashboard()
            elif self.path == "/health":
                self._send_json({"status": "ok", "profile": pm.current})
            elif self.path == "/status":
                self._send_json(pm.mgr.status())
            elif self.path == "/profiles":
                self._send_json(pm.mgr.list_profiles())
            elif self.path == "/system":
                self._send_json(self._system_info())
            elif self.path == "/history":
                self._send_json(pm.mgr.state.get_history(limit=30))
            else:
                self._send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            log.error("GET %s error: %s", self.path, e)

    def do_POST(self):
        pm = self.proxy
        try:
            if self.path in ("/v1/chat/completions", "/v1/completions"):
                self._handle_chat(pm)
            elif self.path == "/switch":
                self._handle_switch(pm)
            elif self.path == "/reset":
                self._handle_reset(pm)
            elif self.path == "/reconcile":
                self._handle_reconcile(pm)
            else:
                self._send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            log.error("POST %s error: %s", self.path, e)

    def _serve_dashboard(self):
        body = None
        static_dir = Path(__file__).parent / "static"
        dashboard = static_dir / "index.html"
        if dashboard.exists():
            body = dashboard.read_bytes()
        if body is None:
            try:
                from edge_llm.dashboard import DASHBOARD_HTML
                body = DASHBOARD_HTML.encode("utf-8")
            except ImportError:
                pass
        if body is None:
            body = (
                "<!DOCTYPE html><html><head><title>EdgeLLM</title>"
                "<style>body{font-family:sans-serif;background:#0f1117;color:#e2e8f0;padding:24px}"
                "h1{color:#3b82f6}</style></head><body>"
                "<h1>EdgeLLM</h1><p>Dashboard unavailable. Use <code>edge-llm status</code></p>"
                "</body></html>"
            ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self._safe_write(body)

    def _system_info(self):
        info = {"cpu_percent": 0, "cpu_cores": os.cpu_count() or 1,
                "ram_total_gb": 0, "ram_used_gb": 0, "uptime_seconds": 0}
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
        return info

    def _read_body(self):
        """Read request body, return parsed JSON or None on error."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                return {}
            body = self.rfile.read(content_length)
            return json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json({"error": f"Invalid JSON: {e}"}, 400)
            return None

    def _handle_chat(self, pm):
        """Forward chat/completions to upstream vLLM with streaming support."""
        data = self._read_body()
        if data is None:
            return

        model = data.get("model", "vllm_qwen27b")
        stream = data.get("stream", False)
        profile = pm.model_to_profile(model)

        if profile and AUTO_SWITCH:
            pm.ensure_profile(profile)

        target_port = pm.get_target_port(model)
        if not target_port:
            self._send_json({"error": f"Unknown model: {model}"}, 404)
            return

        # Forward using HTTPConnection (connection reuse)
        body = json.dumps(data).encode("utf-8")
        try:
            conn = pm.get_upstream(target_port)
            conn.request("POST", self.path, body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
        except Exception as e:
            log.error("Forward to :%d failed: %s", target_port, e)
            # Discard stale connection
            pm._upstream_pool.pop(target_port, None)
            self._send_json({"error": str(e)}, 502)
            return

        try:
            resp_status = resp.status
            resp_headers = dict(resp.getheaders())
            resp_ct = resp_headers.get("Content-Type", "text/plain")

            if stream:
                # Streaming: forward chunk-by-chunk
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
            else:
                # Non-streaming: read full body, send once
                resp_body = resp.read()
                self.send_response(resp_status)
                self.send_header("Content-Type", resp_ct)
                self.send_header("Content-Length", str(len(resp_body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self._safe_write(resp_body)

            # Drain remaining response for connection reuse
            try:
                resp.read()
            except Exception:
                pass

        except Exception as e:
            log.error("Error forwarding response: %s", e)
            try:
                self._send_json({"error": str(e)}, 502)
            except Exception:
                pass

    def _handle_switch(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("profile")
        if not target:
            self._send_json({"error": "Missing profile"}, 400)
            return
        result = pm.mgr.switch(target)
        self._send_json(result)

    def _handle_reset(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("profile", "idle")
        result = pm.mgr.force_reset(target)
        self._send_json(result)

    def _handle_reconcile(self, pm):
        result = pm.mgr.reconcile()
        self._send_json(result)

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
        except BrokenPipeError:
            pass
        except Exception:
            pass


# ─── Threaded HTTP Server ───────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    timeout = 1.0  # for handle_request() to check shutdown flag


# ─── Main ─────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    mgr = ProxyManager()
    shutdown_event = threading.Event()

    server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
    server.proxy_mgr = mgr

    # ── systemd notify ─────────────────────────────────────────
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

    # ── Signal handler (only sets Event, no deadlock) ──────────
    def handle_signal(signum, frame):
        log.info("Received signal %s, initiating shutdown", signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # ── Watchdog thread (independent of health_check) ──────────
    def watchdog_loop():
        while not shutdown_event.is_set():
            shutdown_event.wait(WATCHDOG_INTERVAL)
            if not shutdown_event.is_set():
                sd_notify("WATCHDOG=1")

    if _notify_enabled:
        threading.Thread(target=watchdog_loop, daemon=True, name="watchdog").start()
        log.info("Watchdog thread started (interval=%ds)", WATCHDOG_INTERVAL)

    # ── Health check thread ────────────────────────────────────
    def health_loop():
        while not shutdown_event.is_set():
            shutdown_event.wait(HEALTH_CHECK_INTERVAL)
            if not shutdown_event.is_set():
                mgr.health_check()

    threading.Thread(target=health_loop, daemon=True, name="health").start()

    # ── READY ──────────────────────────────────────────────────
    sd_notify("READY=1")
    log.info("EdgeLLM Proxy: %s:%d (auto_switch=%s, threaded)", PROXY_HOST, PROXY_PORT, AUTO_SWITCH)
    log.info("Dashboard: http://%s:%d/", PROXY_HOST, PROXY_PORT)
    log.info("Current profile: %s", mgr.current)

    # ── Main loop: handle_request + shutdown check ─────────────
    # This replaces serve_forever() to avoid the shutdown() deadlock.
    # handle_request() respects server.timeout (1s), so we check
    # shutdown_event every second.
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
        # Close upstream connections
        for port, conn in mgr._upstream_pool.items():
            try:
                conn.close()
            except Exception:
                pass
        sd_notify("STOPPING=1")
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()
