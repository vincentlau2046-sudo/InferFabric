"""
inferfabric/forwarder.py — Forwarding logic extracted from ProxyHandler.

All functions accept a `handler` parameter (ProxyHandler instance) and
use its HTTP response methods (send_response, send_header, end_headers,
wfile.write, wfile.flush) to send data to the client.
"""

import json
import logging
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
from inferfabric.proxy.request_logger import RequestLog
from inferfabric.proxy.sse_buffer import SSELineBuffer


@dataclass
class CloudResult:
    """cloud 路由请求结果 — 供 RequestLog 补全"""
    status: int = 200
    usage: dict = field(default_factory=dict)  # {prompt_tokens, completion_tokens}
    ttft_ms: float | None = None
    duration_ms: float = 0.0
    error: str | None = None

log = logging.getLogger("inferfabric.forwarder")


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


def pipe_stream_response(handler, resp, sse_buf=None):
    """Pipe SSE stream response to client (CCR-style streaming).

    sse_buf (optional): SSELineBuffer 旁路观察器 — 每个 chunk 写入客户端后
    喂入 buffer 提取 usage；finally 里 flush。不传 sse_buf 时行为与原来完全一致
    （本地 Anthropic 路径不受影响）。
    """
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
                # G-1b: 旁路观察 — 零延迟透传不变，喂入 buffer 提取 usage
                if sse_buf is not None:
                    sse_buf.feed(chunk)
            except (BrokenPipeError, ConnectionResetError):
                log.info("Client disconnected during stream forwarding")
                break
    finally:
        if sse_buf is not None:
            sse_buf.flush()
        resp.close()


# ── JSON response handling ──


def handle_json_response(handler, resp, model_obj, original_model, data, auth_header):
    """Handle non-streaming JSON response; propagate upstream status (unified with streaming)."""
    resp_status = resp.status
    resp_body = resp.read()
    if resp_status != 200:
        log.warning("Local %s returned %d (non-streaming)",
                    model_obj.name, resp_status)
        data["model"] = original_model
        resp.close()
        # 透传上游真实状态码（与流式路径 pipe_stream_response 统一），不再一律压成 502
        try:
            payload = json.loads(resp_body)
        except (json.JSONDecodeError, ValueError):
            payload = {"error": f"Local model returned {resp_status}"}
        send_json(handler, payload, resp_status)
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
            # G-1b: 流式 usage 提取 — 旁路观察 SSE 事件，复用 first_byte_time 记 TTFT
            sse_buf = SSELineBuffer()
            pipe_stream_response(handler, resp, sse_buf)
            return CloudResult(
                status=200,
                usage=dict(sse_buf.usage),
                ttft_ms=(first_byte_time - start) * 1000,
                duration_ms=(time.monotonic() - start) * 1000,
            )
        else:
            resp_body = resp.read()
            result = json.loads(resp_body)
            resp.close()
            send_json(handler, result)
            usage = result.get("usage", {})
            # Normalize usage keys: Baidu Anthropic returns input_tokens/output_tokens,
            # OpenAI returns prompt_tokens/completion_tokens. Unify to OpenAI naming.
            if "input_tokens" in usage and "prompt_tokens" not in usage:
                usage["prompt_tokens"] = usage.get("input_tokens", 0)
            if "output_tokens" in usage and "completion_tokens" not in usage:
                usage["completion_tokens"] = usage.get("output_tokens", 0)
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


# ── PR-ctx: context window guard (metadata-driven) ──


def estimate_tokens(data: dict) -> int:
    """Rough token estimate: len(text)/4 (chars→tokens, no tokenizer).

    粗略估算（user-confirmed）。覆盖 system / messages（str 或 content blocks）/ tools。
    """
    total = 0
    system = data.get("system")
    if isinstance(system, str):
        total += max(len(system) // 4, 1)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                total += max(len(block.get("text", "")) // 4, 1)
    for msg in data.get("messages", []) or []:
        content = msg.get("content")
        if isinstance(content, str):
            total += max(len(content) // 4, 1)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    total += max(len(block.get("text", "")) // 4, 1)
                elif block.get("type") == "image":
                    total += 1000  # 每图块固定粗估
    for tool in data.get("tools", []) or []:
        total += max(len(json.dumps(tool, ensure_ascii=False)) // 4, 1)
    return total


# ── context-window guard: exact tokenization + 500-overflow detection ──

_CONTEXT_OVERFLOW_SIGS = (
    "maximum context length",
    "your prompt contains",
    "context length is",
)


def _is_context_overflow(body_text: str) -> bool:
    low = body_text.lower()
    return any(s in low for s in _CONTEXT_OVERFLOW_SIGS)


def _data_to_prompt(data: dict) -> str:
    """Flatten system + messages + tools into one prompt string for exact tokenization."""
    parts = []
    system = data.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
    for msg in data.get("messages", []) or []:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
    for tool in data.get("tools", []) or []:
        parts.append(json.dumps(tool, ensure_ascii=False))
    return "\n".join(parts)


def _vllm_tokenize(model_obj, data: dict):
    """Exact input token count via vLLM /tokenize (engine-side BPE). Returns int, or None if unavailable."""
    prompt = _data_to_prompt(data)
    if not prompt:
        return 0
    body = json.dumps({"prompt": prompt}).encode("utf-8")
    conn = None
    try:
        conn = HTTPConnection("127.0.0.1", model_obj.port, timeout=30)
        conn.request("POST", "/tokenize", body=body,
                      headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            resp.read()
            resp.close()
            return None
        payload = json.loads(resp.read().decode("utf-8", "replace"))
        resp.close()
        return payload.get("count")
    except Exception as e:
        log.warning("vLLM /tokenize failed for %s: %s", model_obj.name, e)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _pipe_raw(handler, status: int, raw: bytes):
    """Pipe a fully-read raw body back to the client as a single chunked stream."""
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    if raw:
        handler.wfile.write(f"{len(raw):x}\r\n".encode())
        handler.wfile.write(raw)
        handler.wfile.write(b"\r\n")
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()


def check_context_window(handler, data: dict, model_obj) -> bool:
    """PR-ctx: context window guard — 混合策略。

    元数据驱动：上限来自 model_obj.max_context_len（模型 YAML 声明）。
    - 未声明 (None) → 不强制，直接放行。
    - len/4 粗估 ≤ 预算 90% → 直接放行（零成本快路径）。
    - 粗估 > 预算 → 直接 413。
    - 粗估落在 (90%, 100%) 灰区 → 调 vLLM /tokenize 拿精确 token 数，
      精确输入 + max_tokens > max_ctx 则 413。
    返回 True=放行，False=已发 413。
    """
    max_ctx = model_obj.max_context_len
    if not max_ctx:
        return True
    max_output = data.get("max_tokens") or data.get("max_completion_tokens") or 8192
    input_budget = max_ctx - max_output
    if input_budget <= 0:
        send_json(handler, {
            "error": f"Context window exceeded: requested output {max_output} tokens >= context limit {max_ctx} (model: {model_obj.name})",
            "type": "context_window_exceeded",
        }, 413)
        return False
    est = estimate_tokens(data)
    if est > input_budget:
        send_json(handler, {
            "error": (
                f"Context window exceeded: estimated {est} input tokens + "
                f"{max_output} output tokens > {max_ctx} context limit "
                f"(model: {model_obj.name})"
            ),
            "type": "context_window_exceeded",
        }, 413)
        return False
    if est > int(input_budget * 0.9):
        exact = _vllm_tokenize(model_obj, data)
        if exact is not None and exact + max_output > max_ctx:
            send_json(handler, {
                "error": (
                    f"Context window exceeded: exact input {exact} + output {max_output} "
                    f"= {exact + max_output} > {max_ctx} (model: {model_obj.name})"
                ),
                "type": "context_window_exceeded",
            }, 413)
            return False
    return True


# ── Local forward with retry chain ──


def forward_anthropic_local(handler, pm, data, auth_header, model_obj, original_model):
    """CCR-style retry chain: local vLLM + exponential backoff → error on exhaustion."""
    was_stream = data.get("stream", False)
    data["model"] = model_obj.served_name or "vllm_qwen27b"
    # PR-ctx: 元数据驱动 context 守卫 — 超限 413 拒绝，不转发
    if not check_context_window(handler, data, model_obj):
        pm.logger.log(RequestLog(
            req_id=pm.new_request_id(),
            key_name=pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous",
            model=data.get("model", ""),
            status=413, error="context_window_exceeded", route="local",
        ))
        return
    body = json.dumps(data).encode("utf-8")

    last_error = None
    for attempt in range(UPSTREAM_LOCAL_RETRIES + 1):
        conn = None
        try:
            conn = HTTPConnection("127.0.0.1", model_obj.port, timeout=300)
            conn.request("POST", "/v1/messages", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()

            if resp.status == 500:
                raw = b""
                try:
                    raw = resp.read()
                except Exception:
                    pass
                resp.close()
                text = raw.decode("utf-8", "replace")
                if _is_context_overflow(text):
                    # 上下文超限是确定性客户端错误 —— 回 413，不重试
                    send_json(handler, {
                        "error": "Context window exceeded: upstream (vLLM) rejected the prompt — input + requested output exceeds the model context limit",
                        "type": "context_window_exceeded",
                    }, 413)
                    pm.logger.log(RequestLog(
                        req_id=pm.new_request_id(),
                        key_name=pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous",
                        model=data.get("model", ""),
                        status=413, error="context_window_exceeded", route="local",
                    ))
                    return
                if attempt < UPSTREAM_LOCAL_RETRIES:
                    delay_s = exponential_backoff(attempt)
                    log.warning("Local %s returned 500, retry #%d in %.1fs",
                                model_obj.name, attempt, delay_s)
                    time.sleep(delay_s)
                    continue
                # 最终次：把 500 原样透传（统一流式/非流式状态）
                if was_stream:
                    _pipe_raw(handler, 500, raw)
                else:
                    try:
                        send_json(handler, json.loads(text), 500)
                    except (json.JSONDecodeError, ValueError):
                        send_json(handler, {"error": "Local model returned 500"}, 500)
                return

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

    log.error("Local model failed after all retries: %s", last_error)
    send_json(handler, {"error": f"Local model unreachable: {last_error}"}, 503)
