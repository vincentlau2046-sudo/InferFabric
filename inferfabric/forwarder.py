"""
inferfabric/forwarder.py — Forwarding logic extracted from ProxyHandler.

All functions accept a `handler` parameter (ProxyHandler instance) and
use its HTTP response methods (send_response, send_header, end_headers,
wfile.write, wfile.flush) to send data to the client.
"""

import json
import logging
import os
import time
from http.client import HTTPConnection
from urllib.request import Request, urlopen
from urllib.error import HTTPError as _HTTPError

from inferfabric.config import (
    UPSTREAM_LOCAL_RETRIES,
    exponential_backoff,
    should_retry_on_status,
)

log = logging.getLogger("inferfabric.forwarder")


# ── Baidu fallback config ──

BAIDU_MESSAGES_BASE = os.environ.get(
    "BAIDU_MESSAGES_BASE", "https://qianfan.baidubce.com/anthropic/coding/v1"
)
BAIDU_TIMEOUT = 60


# ── Local model type filter ──

LOCAL_LLM_TYPES = {"llm", "vl"}


# ── Response helpers ──


def send_json(handler, body_d, status=200, extra_headers=None):
    """Send JSON response with CORS headers."""
    body = json.dumps(body_d, ensure_ascii=False).encode()
    try:
        handler.send_response(status)
        if extra_headers:
            for k, v in extra_headers.items():
                if v is None:
                    continue
                handler.send_header(k, str(v))
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def read_body(handler):
    """Read and parse JSON request body. Returns dict or None on error."""
    try:
        content_length = int(handler.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        if content_length > 10 * 1024 * 1024:  # 10MB limit
            send_json(handler, {"error": "payload too large (max 10MB)"}, 413)
            return None
        raw = handler.rfile.read(content_length)
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        send_json(handler, {"error": f"Invalid JSON: {e}"}, 400)
        return None


# ── Stream forwarding ──


def pipe_stream_response(handler, resp):
    """Pipe SSE stream response to client (CCR-style streaming)."""
    handler.send_response(resp.status)
    for h in ("content-type", "cache-control", "x-request-id"):
        val = resp.getheader(h)
        if val:
            handler.send_header(h, val)
    handler.end_headers()
    try:
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                log.info("Client disconnected during stream forwarding")
                break
    finally:
        resp.close()


# ── JSON response with Baidu fallback ──


def handle_json_response(handler, resp, model_obj, original_model, data, auth_header):
    """Handle non-streaming JSON response with Baidu fallback on non-200."""
    resp_status = resp.status
    resp_body = resp.read()
    if resp_status != 200:
        log.warning("Local %s returned %d (non-streaming) — falling back to Baidu",
                    model_obj.name, resp_status)
        data["model"] = original_model
        resp.close()
        forward_to_baidu(handler, data, auth_header, original_model)
        return
    try:
        result = json.loads(resp_body)
        send_json(handler, result)
    except json.JSONDecodeError:
        send_json(handler, {"error": "invalid response from local model"}, 502)


# ── Baidu fallback ──


def forward_to_baidu(handler, data, auth_header, original_model):
    """Forward Anthropic Messages request to Baidu Coding Plan."""
    was_stream = data.pop("stream", None)
    body = json.dumps(data).encode("utf-8")
    url = f"{BAIDU_MESSAGES_BASE}/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": auth_header.replace("Bearer ", "").replace("bearer ", "").strip(),
    }

    try:
        req = Request(url, data=body, headers=headers, method="POST")
        resp = urlopen(req, timeout=BAIDU_TIMEOUT)
        resp_body = resp.read()
        result = json.loads(resp_body)
        send_json(handler, result)
    except _HTTPError as e:
        log.error("Baidu fallback failed: %s %s", e.code, e.reason)
        error_body = e.read().decode("utf-8", errors="replace")
        send_json(handler, {"error": f"Baidu fallback failed: {error_body}"}, 502)
    except Exception as e:
        log.error("Baidu fallback error: %s", e)
        send_json(handler, {"error": f"Baidu unreachable: {e}"}, 503)


# ── Local forward with retry chain ──


def forward_anthropic_local(handler, pm, data, auth_header, model_obj, original_model):
    """CCR-style retry chain: local vLLM + exponential backoff → Baidu fallback."""
    was_stream = data.get("stream", False)
    data["model"] = model_obj.served_name or "vllm_qwen27b"
    body = json.dumps(data).encode("utf-8")

    last_error = None
    for attempt in range(UPSTREAM_LOCAL_RETRIES + 1):
        conn = None
        try:
            conn = HTTPConnection("127.0.0.1", model_obj.port, timeout=300)
            conn.request("POST", "/v1/messages", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()

            if should_retry_on_status(resp.status) and attempt < UPSTREAM_LOCAL_RETRIES:
                try:
                    resp.read()
                except Exception:
                    pass
                resp.close()
                delay_s = exponential_backoff(attempt)
                log.warning("Local %s returned %d, retry #%d in %.1fs",
                            model_obj.name, resp.status, attempt, delay_s)
                time.sleep(delay_s)
                continue

            if was_stream:
                pipe_stream_response(handler, resp)
            else:
                handle_json_response(handler, resp, model_obj, original_model, data, auth_header)
            return

        except (ConnectionRefusedError, ConnectionResetError, OSError, BrokenPipeError) as e:
            last_error = e
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            if attempt < UPSTREAM_LOCAL_RETRIES:
                delay_s = exponential_backoff(attempt)
                log.warning("Local %s connection failed (attempt %d/%d): %s — retry in %.1fs",
                            model_obj.name, attempt + 1, UPSTREAM_LOCAL_RETRIES + 1, e, delay_s)
                time.sleep(delay_s)
                continue
            log.error("Local %s failed after %d attempts: %s",
                       model_obj.name, UPSTREAM_LOCAL_RETRIES + 1, e)

        except Exception as e:
            log.error("Local %s unexpected error: %s", model_obj.name, e)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            break

    log.info("Falling back to Baidu after local failure (last error: %s)", last_error)
    data["model"] = original_model
    forward_to_baidu(handler, data, auth_header, original_model)
