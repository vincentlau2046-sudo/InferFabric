"""
inferfabric/proxy/chat_handlers.py — Chat completion handlers.

Extracted from proxy.py (v4.1 P3 split).
"""

import json
import time
import uuid
import logging
from inferfabric.ratelimit import _get_model_rate_limiter
from inferfabric.proxy_manager import AUTO_SWITCH

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
            except Exception:
                pass
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
                        except json.JSONDecodeError:
                            pass
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
    model = data.get("model", "vllm_qwen27b")
    log.debug("Incoming request: model=%s", model)
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
    limiter = _get_model_rate_limiter(pm, model)
    if not limiter.acquire():
        handler._send_json(
            {"error": "vLLM at capacity, try again later", "status": "rate_limit"},
            429,
        )
        return
    try:
        for attempt in range(2):
            if _forward_request(handler, pm, target_port, body, stream):
                return
            if attempt == 0:
                time.sleep(0.5)
        handler._send_json({"error": "Upstream unavailable after retry"}, 502)
    finally:
        limiter.release()


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
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    size = f"{len(chunk):x}\r\n".encode()
                    handler._safe_write(size)
                    handler._safe_write(chunk)
                    handler._safe_write(b"\r\n")
                handler._safe_write(b"0\r\n\r\n")
            except Exception as e:
                log.debug("Stream forwarding interrupted: %s", e)
            finally:
                resp.close()
        else:
            try:
                resp_body = resp.read()
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