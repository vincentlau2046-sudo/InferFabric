"""
inferfabric/proxy/chat_handlers.py — Chat completion handlers.

Extracted from proxy.py (v4.1 P3 split).
"""

import json
import time
import uuid
import logging
from inferfabric.proxy_manager import AUTO_SWITCH
from inferfabric import forwarder
from inferfabric.proxy.sse_buffer import SSELineBuffer
from inferfabric.proxy.request_logger import RequestLog

log = logging.getLogger("inferfabric.proxy.chat")


def handle_ollama_native(handler, pm, data, target_port, model_obj):
    """Handle chat for Ollama backends using native /api/chat API.

    This replaces the old ProxyHandler._handle_chat_ollama_native method.
    Called directly by the handler; uses handler._send_json and handler._safe_write.
    """
    stream = data.get("stream", False)
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
        handler._send_json({"error": str(e)}, 502)
        if conn:
            try:
                conn.close()
            except Exception as e:
                log.warning("Chat handler error: %s", e)
        return

    try:
        resp_status = resp.status
        if resp_status != 200:
            err_body = resp.read().decode("utf-8", errors="replace")
            handler._send_json({"error": f"Ollama error: {err_body[:500]}", }, resp_status)
            return

        if stream:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
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
                                handler._safe_write(f"data: {sse_data}\n\n".encode())
                        except json.JSONDecodeError as e:
                            log.warning("JSON decode error: %s", e)
                sse_done = json.dumps({
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": data["model"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                })
                handler._safe_write(f"data: {sse_done}\n\n".encode())
                handler._safe_write(b"data: [DONE]\n\n")
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
            handler._send_json({
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
        handler._send_json({"error": str(e)}, 500)
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


def handle_chat(handler, pm, data):
    """Handle OpenAI chat completions request.

    Replaces ProxyHandler._handle_chat. Dispatches to either
    handle_ollama_native or the vLLM forwarding path.
    """
    model = data.get("model")
    if not model:
        handler._send_json({"error": "model field is required", "status": "bad_request"}, 400)
        return
    log.debug("Incoming request: model=%s", model)

    # PR-B: Request context for logging
    req_id = pm.new_request_id()
    handler._req_start = time.monotonic()
    auth_header = handler.headers.get("Authorization", "") or handler.headers.get("x-api-key", "")
    key_name = pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous"

    # G-1b: Initialize usage (before any early return)
    handler._usage = {"prompt_tokens": 0, "completion_tokens": 0}

    # PR-A: Auth check
    if pm.auth.enabled:
        model_for_auth = model.split("/")[-1] if "/" in model else model
        auth_ok, auth_reason = pm.auth.check(auth_header, model_for_auth)
        if not auth_ok:
            pm.logger.log(RequestLog(
                req_id=req_id, key_name=key_name, model=model_for_auth,
                status=401, error=auth_reason, duration_ms=(time.monotonic()-handler._req_start)*1000,
            ))
            handler._send_json({"error": auth_reason, "status": "unauthorized"}, 401)
            return

    # PR-G2: 流式请求自动注入 include_usage=true — 确保 token 提取生效
    # vLLM 默认不在 SSE 流中返回 usage，需客户端传 stream_options.include_usage=true
    # IFF 代理层透明注入，对客户端无感
    if data.get("stream", False):
        if "stream_options" not in data:
            data["stream_options"] = {}
        data["stream_options"].setdefault("include_usage", True)

    stream = data.get("stream", False)

    # Auto-switch
    service_name = pm.model_to_service(model)
    if service_name and AUTO_SWITCH:
        switched = pm.ensure_service(service_name)
        if switched is None:
            handler._send_json({"error": "switch already in progress", "status": "conflict"}, 409)
            return
        if not switched and service_name not in pm.mgr.active_services:
            if pm.mgr.state.is_manually_stopped(service_name):
                reason = f"{service_name} was manually stopped — auto-switch blocked for {pm.mgr.state.MANUAL_STOP_TTL}s"
            else:
                reason = "tri-state rule violation or switch in progress"
            handler._send_json({"error": f"Cannot switch to {reason}"}, 503)
            return

    target_port = pm.get_target_port(model)
    if not target_port:
        # PR-D: Check cloud routing before returning 404
        pm.ensure_cloud_discovered()
        local_model_names = {m.served_name for m in pm.mgr._models.values() if m.served_name}
        route = pm.cloud.resolve_route(model, local_model_names)
        if route and route.startswith("cloud:"):
            provider_name = route.split(":", 1)[1]
            provider_cfg = pm.cloud.get_provider_config(provider_name)
            short_name = model.split("/")[-1] if "/" in model else model
            cloud_model = (pm.cloud.cloud_models.get(f"{provider_name}/{short_name}")
                          or pm.cloud.cloud_models.get(short_name))
            if provider_cfg and cloud_model:
                log.info("/v1/chat/completions → CLOUD %s [model=%s]", provider_name, model)
                result = forwarder.forward_to_cloud(
                    handler, data, provider_cfg, cloud_model,
                    protocol="openai", original_model=model,
                )
                pm.logger.log(RequestLog(
                    model=model, status=result.status, route=f"cloud:{provider_name}",
                    key_name=key_name, req_id=req_id,
                    cloud_provider=provider_name,
                    tokens_in=result.usage.get("prompt_tokens", 0),
                    tokens_out=result.usage.get("completion_tokens", 0),
                    ttft_ms=result.ttft_ms,
                    duration_ms=result.duration_ms,
                    error=result.error,
                ))
                return
        handler._send_json({"error": f"Unknown model: {model}"}, 404)
        return

    # Rewrite model name to upstream's served_name
    ollama_model_obj = None
    if service_name:
        model_obj = pm.mgr.get_model(service_name)
        if model_obj and model_obj.served_name:
            data["model"] = model_obj.served_name
        if model_obj and (model_obj.is_ollama or model_obj.is_ollama_cpp):
            ollama_model_obj = model_obj

    if ollama_model_obj and (ollama_model_obj.is_ollama or ollama_model_obj.is_ollama_cpp):
        handle_ollama_native(handler, pm, data, target_port, ollama_model_obj)
        return

    # vLLM path — apply dynamic rate limiter
    body = json.dumps(data).encode("utf-8")
    # v4.6.3: 使用配置的 timeout (observe 模式下不会 429)
    gate = pm.dual_gate.acquire(model)
    if not gate.ok:
        pm.logger.log(RequestLog(
            req_id=req_id, key_name=key_name, model=model,
            status=429, error=gate.reason, route="local",
            duration_ms=(time.monotonic()-handler._req_start)*1000,
        ))
        handler._send_json(
            {"error": f"Rate limited: {gate.reason}", "status": "rate_limit"},
            429,
        )
        return
    try:
        for attempt in range(2):
            if _forward_request(handler, pm, target_port, body, stream):
                # PR-B: Log successful request with TTFT + usage
                ttft = getattr(handler, '_ttft_ms', None)
                usage = getattr(handler, '_usage', {})
                pm.logger.log(RequestLog(
                    req_id=req_id, key_name=key_name, model=model,
                    status=200, ttft_ms=ttft, route="local",
                    tokens_in=usage.get("prompt_tokens", 0),
                    tokens_out=usage.get("completion_tokens", 0),
                    duration_ms=(time.monotonic()-handler._req_start)*1000,
                ))
                return
            if attempt == 0:
                time.sleep(0.5)
        pm.logger.log(RequestLog(
            req_id=req_id, key_name=key_name, model=model,
            status=502, error="upstream_unavailable", route="local",
            duration_ms=(time.monotonic()-handler._req_start)*1000,
        ))
        handler._send_json({"error": "Upstream unavailable after retry"}, 502)
    finally:
        gate.release()


def _forward_request(handler, pm, target_port, body, stream):
    """Forward a request to an upstream service.

    Returns True if the response was fully sent to the client.
    Returns False if the caller should retry (headers not yet sent).
    """
    headers_sent = False
    conn = None
    resp = None
    try:
        conn = pm.make_conn(target_port)
        conn.request("POST", handler.path, body=body,
                      headers={"Content-Type": "application/json"})
        resp = conn.getresponse()

        resp_status = resp.status
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        resp_ct = resp_headers.get("content-type", "application/json")

        if stream:
            headers_sent = True
            handler.send_response(resp_status)
            handler.send_header("Content-Type", resp_ct)
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
            # PR-B: TTFT tracking — record time of first chunk
            ttft_recorded = False
            sse_buf = SSELineBuffer()  # G-1b: SSE usage extractor
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    # PR-B: Record TTFT on first data chunk
                    if not ttft_recorded:
                        ttft_recorded = True
                        if hasattr(handler, '_req_start'):
                            handler._ttft_ms = (time.monotonic() - handler._req_start) * 1000
                    size = f"{len(chunk):x}\r\n".encode()
                    handler._safe_write(size)
                    handler._safe_write(chunk)
                    handler._safe_write(b"\r\n")
                    # G-1b: 旁路观察提取 usage
                    sse_buf.feed(chunk)
                handler._safe_write(b"0\r\n\r\n")
            except Exception as e:
                log.debug("Stream forwarding interrupted: %s", e)
            finally:
                # G-1b: flush 残余 + 保存 usage（即使异常也保留已提取的部分）
                sse_buf.flush()
                handler._usage = dict(sse_buf.usage)
                resp.close()
        else:
            # PR-B: TTFT for non-streaming
            if hasattr(handler, '_req_start'):
                handler._ttft_ms = (time.monotonic() - handler._req_start) * 1000
            try:
                resp_body = resp.read()
                # G-1b: 非流式 — 解析 JSON body 提取 usage
                try:
                    body_obj = json.loads(resp_body)
                    usage = body_obj.get("usage", {})
                    if usage:
                        handler._usage["prompt_tokens"] = usage.get("prompt_tokens", 0) or 0
                        handler._usage["completion_tokens"] = usage.get("completion_tokens", 0) or 0
                except (json.JSONDecodeError, AttributeError):
                    pass
            finally:
                resp.close()
            headers_sent = True
            handler.send_response(resp_status)
            handler.send_header("Content-Type", resp_ct)
            handler.send_header("Content-Length", str(len(resp_body)))
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.end_headers()
            handler._safe_write(resp_body)
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
