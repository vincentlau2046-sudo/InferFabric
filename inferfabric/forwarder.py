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
from dataclasses import dataclass, field
from http.client import HTTPConnection
from urllib.request import Request, urlopen
from urllib.error import HTTPError as _HTTPError

from inferfabric.config import (
    UPSTREAM_LOCAL_RETRIES,
    exponential_backoff,
    should_retry_on_status,
)


@dataclass
class CloudResult:
    """cloud 路由请求结果 — 供 RequestLog 补全"""
    status: int = 200
    usage: dict = field(default_factory=dict)  # {prompt_tokens, completion_tokens}
    ttft_ms: float | None = None
    duration_ms: float = 0.0
    error: str | None = None

log = logging.getLogger("inferfabric.forwarder")


# ── Baidu fallback config ──

BAIDU_MESSAGES_BASE = os.environ.get(
    "BAIDU_MESSAGES_BASE", "https://qianfan.baidubce.com/anthropic/coding/v1"
)
BAIDU_TIMEOUT = 60


# ── Local model type filter ──

LOCAL_LLM_TYPES = {"llm", "vl", "omni"}


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
    # CORS — required for browser-based clients
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, x-api-key, anthropic-version")
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


# ── Cloud provider forwarding (PR-D) ──


def forward_to_cloud(handler, data, provider_cfg, cloud_model, protocol="openai", original_model=None):
    """双协议透传：OpenAI → cloud OpenAI endpoint, Anthropic → cloud Anthropic endpoint。

    IFF 不做协议转换，客户端用什么协议发，就往对应的云端端点转发。
    IFF 持有云端凭证，客户端只需 IFF key。

    Returns CloudResult for request logging (G-1a).
    """
    start = time.monotonic()

    if protocol == "anthropic":
        if not cloud_model.anthropic_available:
            err = f"Provider {provider_cfg.name} does not support Anthropic protocol"
            send_json(handler, {"error": err}, 501)
            return CloudResult(status=501, error=err)
        if not provider_cfg.anthropic_base:
            err = f"Provider {provider_cfg.name} has no Anthropic base configured"
            send_json(handler, {"error": err}, 501)
            return CloudResult(status=501, error=err)
        url = f"{provider_cfg.anthropic_base.rstrip('/')}/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": provider_cfg.api_key,
        }
    else:  # openai
        if not cloud_model.openai_available:
            err = f"Provider {provider_cfg.name} does not support OpenAI protocol"
            send_json(handler, {"error": err}, 501)
            return CloudResult(status=501, error=err)
        if not provider_cfg.openai_base:
            err = f"Provider {provider_cfg.name} has no OpenAI base configured"
            send_json(handler, {"error": err}, 501)
            return CloudResult(status=501, error=err)
        url = f"{provider_cfg.openai_base.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider_cfg.api_key}",
        }

    # Override model with cloud model_id
    if original_model and data.get("model") != cloud_model.model_id:
        data["model"] = cloud_model.model_id

    was_stream = data.get("stream", False)
    body = json.dumps(data).encode("utf-8")

    try:
        req = Request(url, data=body, headers=headers, method="POST")
        resp = urlopen(req, timeout=provider_cfg.timeout)
        first_byte_time = time.monotonic()

        if was_stream:
            # Usage tracking for streaming deferred to G-1b
            pipe_stream_response(handler, resp)
            return CloudResult(
                status=200,
                duration_ms=(time.monotonic() - start) * 1000,
            )
        else:
            resp_body = resp.read()
            result = json.loads(resp_body)
            resp.close()
            send_json(handler, result)
            usage = result.get("usage", {})
            return CloudResult(
                status=200,
                usage=usage,
                ttft_ms=(first_byte_time - start) * 1000,
                duration_ms=(time.monotonic() - start) * 1000,
            )
    except _HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        e.close()
        log.error("Cloud %s returned HTTP %d: %s", provider_cfg.name, e.code, error_body[:200])
        # Propagate upstream status code (429→429, 404→404) instead of always 502
        status = e.code if 400 <= e.code < 500 else 502
        err_msg = f"Cloud provider error ({e.code}): {error_body[:500]}"
        send_json(handler, {"error": err_msg}, status)
        return CloudResult(
            status=status,
            error=err_msg,
            duration_ms=(time.monotonic() - start) * 1000,
        )
    except Exception as e:
        log.error("Cloud %s request failed: %s", provider_cfg.name, e)
        err_msg = f"Cloud provider unreachable: {e}"
        send_json(handler, {"error": err_msg}, 503)
        return CloudResult(
            status=503,
            error=err_msg,
            duration_ms=(time.monotonic() - start) * 1000,
        )


# ── Baidu fallback (v1 compat, PR-D 后删除) ──


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
        resp.close()
        send_json(handler, result)
    except _HTTPError as e:
        log.error("Baidu fallback failed: %s %s", e.code, e.reason)
        error_body = e.read().decode("utf-8", errors="replace")
        e.close()
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
