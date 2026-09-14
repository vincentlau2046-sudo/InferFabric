"""
inferfabric/proxy/async_server.py — aiohttp async HTTP server (R7 Step 1)

将 ThreadedHTTPServer 替换为 aiohttp，仍调用同步的 handler 方法，
通过 ThreadPoolExecutor 避免阻塞事件循环。

启动方式：
  python -m inferfabric.proxy --async   # async 模式
  python -m inferfabric.proxy           # threaded 模式（默认）
"""

import asyncio
import io
import logging
import os
import signal as signal_mod
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import web

from inferfabric.proxy.handler import (
    _GET_ROUTES, _POST_ROUTES, _DELETE_ROUTES,
    ProxyHandler,
    PROXY_HOST, PROXY_PORT, WATCHDOG_INTERVAL,
    _validate_admin_token_safety,
)
from inferfabric.proxy_manager import ProxyManager
from inferfabric.watchdog import ModelWatchdog

log = logging.getLogger("inferfabric.async_server")


class _MockServer:
    """仿 ThreadedHTTPServer 的最小替身 — 持有 proxy_mgr。"""
    def __init__(self, pm):
        self.proxy_mgr = pm
        self.server_close = lambda: None


def _build_handler(request, body_bytes, mgr):
    """创建最小 ProxyHandler 实例，替换掉所有 BaseHTTPRequestHandler
    的 socket I/O 方法为纯内存版本。"""
    h = ProxyHandler.__new__(ProxyHandler)
    h.server = _MockServer(mgr)
    h.path = request.path
    h.headers = request.headers
    h.command = request.method
    h.rfile = io.BytesIO(body_bytes)
    h.wfile = io.BytesIO()
    h._req_id = ""
    h._req_start = 0.0
    h._key_name = "anonymous"
    h._usage = {"prompt_tokens": 0, "completion_tokens": 0}
    h._ttft_ms = None

    # I/O 方法替换为内存版本
    h._resp_status = 200
    h._resp_headers = {}

    def _send_response(code, message=None):
        h._resp_status = code

    def _send_header(key, value):
        h._resp_headers[key] = value

    def _end_headers():
        pass

    h.send_response = _send_response
    h.send_header = _send_header
    h.end_headers = _end_headers
    return h


def _build_app(mgr: ProxyManager, watchdog: ModelWatchdog,
               executor: ThreadPoolExecutor) -> web.Application:
    """构建 aiohttp Application。"""
    app = web.Application()

    async def _handle(request: web.Request) -> web.Response:
        body = await request.read()
        h = _build_handler(request, body, mgr)

        # 查找路由
        path = request.path
        if request.method == "GET":
            handler_fn = _GET_ROUTES.get(path)
        elif request.method == "POST":
            handler_fn = _POST_ROUTES.get(path)
        elif request.method == "DELETE" and _DELETE_ROUTES:
            handler_fn = _DELETE_ROUTES.get(path)
        else:
            return web.Response(status=405)

        if handler_fn is None:
            return web.Response(status=404)

        await asyncio.get_event_loop().run_in_executor(executor, handler_fn, h, mgr)

        body_data = h.wfile.getvalue() or b""
        content_type_val = "application/json"
        charset = None
        for k, v in getattr(h, '_resp_headers', {}).items():
            if k.lower() == 'content-type':
                content_type_val = v
        if "; charset=" in content_type_val:
            parts = content_type_val.split("; charset=")
            content_type_val = parts[0].strip()
            charset = parts[1].split(";")[0].strip() if len(parts) > 1 else None

        return web.Response(
            body=body_data,
            status=getattr(h, '_resp_status', 200),
            content_type=content_type_val,
            charset=charset,
        )

    for path in _GET_ROUTES:
        app.router.add_get(path, _handle)
    for path in _POST_ROUTES:
        app.router.add_post(path, _handle)
    if _DELETE_ROUTES:
        for path in _DELETE_ROUTES:
            app.router.add_delete(path, _handle)

    return app


async def _run_async(mgr: ProxyManager, watchdog: ModelWatchdog):
    executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="iff-async")
    app = _build_app(mgr, watchdog, executor)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, PROXY_HOST, PROXY_PORT)

    log.info("Async server starting on %s:%d", PROXY_HOST, PROXY_PORT)
    await site.start()

    shutdown_event = threading.Event()
    def _signal_handler():
        log.info("Received shutdown signal")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (ValueError, RuntimeError, NotImplementedError):
            pass

    _notify_socket = os.environ.get('NOTIFY_SOCKET')
    if _notify_socket:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.connect(_notify_socket)
            sock.sendall(b"READY=1")
            sock.close()
        except Exception:
            pass

    while not shutdown_event.is_set():
        await asyncio.sleep(1)

    log.info("Shutting down async server...")
    await runner.cleanup()
    executor.shutdown(wait=True)
    log.info("Async server stopped")


def start_async():
    """入口：初始化 ProxyManager + Watchdog + 启动 async 服务。"""
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
    watchdog = ModelWatchdog(mgr.mgr, check_interval=30, auto_restart=True)
    watchdog.start()

    from inferfabric.config_reloader import ConfigReloader
    config_reloader = ConfigReloader(mgr.mgr, auth=mgr.auth, cloud=mgr.cloud)
    mgr.config_reloader = config_reloader
    config_reloader.setup()

    try:
        asyncio.run(_run_async(mgr, watchdog))
    except KeyboardInterrupt:
        pass
    finally:
        watchdog.stop()
        mgr.health_monitor.stop()
        log.info("InferFabric async server stopped.")