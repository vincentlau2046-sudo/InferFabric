# tests/unit/proxy/test_admin_stop.py
"""POST /stop 对 exclusive 模型应返回 4xx，而非 200 假成功。"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
# 添加 _deps 路径
_deps = Path(__file__).parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))


def _build_handler_with_exclusive_running():
    """构造 active_services 含 exclusive 模型、gpu_mode=exclusive 的 mock。"""
    from inferfabric.proxy.handler import ProxyHandler
    h = ProxyHandler.__new__(ProxyHandler)
    h.headers = {"Content-Type": "application/json"}
    h.rfile = __import__("io").BytesIO(json.dumps({"model": "Qwen38-27B-TXT"}).encode())
    h._resp_status = 200
    h._resp_headers = {}
    h._headers_locked = False
    # 捕获 send_json 的 status
    sent = {}
    def _send_json(data, status=200, extra_headers=None):
        sent["data"] = data
        sent["status"] = status
    h._send_json = _send_json
    h._read_body = lambda: {"model": "Qwen38-27B-TXT"}
    pm = MagicMock()
    pm.mgr.stop_service.return_value = {
        "status": "error",
        "message": "Cannot stop individual service in exclusive mode. Use 'switch idle'.",
    }
    return h, pm, sent


def test_stop_exclusive_returns_4xx_not_200():
    """A 方案：exclusive stop error 必须 4xx，不能默认 200 谎报成功。"""
    h, pm, sent = _build_handler_with_exclusive_running()
    h._handle_stop(pm)
    assert sent["status"] >= 400, "exclusive stop error 不应返回 200"
    assert sent["data"].get("error") or sent["data"].get("message")


def test_stop_shared_success_returns_200():
    """shared 模型 stop 成功仍返回 200。"""
    h, pm, sent = _build_handler_with_exclusive_running()
    pm.mgr.stop_service.return_value = {"status": "stopped", "model": "ovis-ocr2"}
    h._read_body = lambda: {"model": "ovis-ocr2"}
    h._handle_stop(pm)
    assert sent["status"] == 200
