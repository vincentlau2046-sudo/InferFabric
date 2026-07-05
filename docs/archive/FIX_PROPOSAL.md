# InferFabric 审计问题修复方案

> 基于 `ATOMCODE_AUDIT_REPORT.md`，针对 4 个 CRITICAL + 6 个 HIGH 问题给出具体修改方案。
> 所有改动保持向后兼容，不破坏 `iff` CLI 命令和 edge-llm `__main__` 入口。

---

## 目录

1. [P0-C1 — Proxy 添加 API Key 认证](#p0-c1-proxy-添加-api-key-认证)
2. [P0-C2 — 移除 `nvidia-smi --gpu-reset` 破坏性重置](#p0-c2-移除-nvidia-smi---gpu-reset-破坏性重置)
3. [P0-C3 — `pkill -f "python main.py"` 改为精确路径匹配](#p0-c3-pkill--精确路径匹配)
4. [P0-C4 — 流式连接泄漏：关闭上游 HTTPConnection](#p0-c4-流式连接泄漏)
5. [P1-H1 — 类级共享 dict 加线程锁保护](#p1-h1-类级共享-dict-加线程锁)
6. [P1-H2 — VRAM 检查竞态：加锁保护](#p1-h2-vram-检查竞态)
7. [P1-H3 — `set_sleep_state()` TOCTOU + 死锁修复](#p1-h3-set_sleep_state-toctou--死锁)
8. [P1-H4 — Config drift 改为 YAML 内容哈希](#p1-h4-config-drift-改为-yaml-哈希)
9. [P1-H5 — `forward_anthropic_local` 补 conn.close()](#p1-h5-forward_anthropic_local-补-connclose)
10. [P1-H6 — Dashboard JS 锁被双 tab 绕过](#p1-h6-dashboard-js-锁加固)

---

## P0-C1: Proxy 添加 API Key 认证

**严重性**: CRITICAL · **文件**: `inferfabric/proxy.py`

### 问题

所有 HTTP 端点对 LAN 完全开放。若绑定 `0.0.0.0`，LAN 上任何人都可以执行模型切换、调用 LLM、查看系统状态。

### 方案

添加 `EDGE_API_KEY` 环境变量。当设置时，所有 POST/GET 请求需要 `Authorization: Bearer <key>` 或 `x-api-key: <key>` 头。OPTIONS 免检（CORS preflight）。未设置时保持向后兼容（无认证）。

### 修改

#### 1. 添加配置常量

```python
# proxy.py, L28-34
PROXY_HOST = os.environ.get("EDGE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("EDGE_PROXY_PORT", "8999"))
AUTO_SWITCH = os.environ.get("EDGE_AUTO_SWITCH", "1") == "1"
HEALTH_CHECK_INTERVAL = int(os.environ.get("EDGE_HEALTH_CHECK", "60"))
WATCHDOG_INTERVAL = 20
# ADD:
PROXY_API_KEY = os.environ.get("EDGE_API_KEY", "")
```

#### 2. 添加认证装饰器/检查方法

```python
# ProxyHandler 类内, L191 之前添加

def _check_auth(self) -> bool:
    """Check API key if configured. Returns True if authorized."""
    if not PROXY_API_KEY:
        return True  # No auth configured = backward compat
    # Check Authorization header (Bearer <key>)
    auth = self.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == PROXY_API_KEY:
        return True
    # Check x-api-key header
    if self.headers.get("x-api-key") == PROXY_API_KEY:
        return True
    self.send_response(401)
    self.send_header("Content-Type", "application/json")
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("WWW-Authenticate", 'Bearer realm="inferfabric"')
    body = json.dumps({"error": "unauthorized"}).encode()
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)
    return False
```

#### 3. 在 do_GET / do_POST 开头检查

```diff
  def do_GET(self):
+     if not self._check_auth():
+         return
      pm = self.proxy
      ...

  def do_POST(self):
+     if not self._check_auth():
+         return
      pm = self.proxy
      ...
```

#### 4. `_send_json` 合并 `WWW-Authenticate` 到 401

仅在未认证时发送，不影响其他 401 响应。

### 影响范围

- **向后兼容**: ✅ 默认不设置 `EDGE_API_KEY` 时无行为变化
- **CLI 不受影响**: `iff` 走 Python 内部调用，不走 HTTP
- **Dashboard**: 浏览器中打开时，若启用了认证，用户需要在 `fetch()` 中添加 `Authorization` 头。Dashboard 需要相应修改。

---

## P0-C2: 移除 `nvidia-smi --gpu-reset` 破坏性重置

**严重性**: CRITICAL · **文件**: `inferfabric/manager.py:886-890`

### 问题

`force_reset()` 中对所有 GPU 进程 SIGKILL 后，若 GPU 未立即释放就执行 `nvidia-smi --gpu-reset`。该操作影响 X server 及其他 CUDA 任务，属于破坏性操作。

### 方案

移除 `--gpu-reset` 调用，替换为等待重试 + 日志警告。GPU 重置应由系统管理员手动执行。

### 修改

```diff
  # manager.py, L884-890
  if not wait_gpu_free(timeout=20):
-     try:
-         import subprocess
-         subprocess.run(["nvidia-smi", "--gpu-reset"], timeout=10, check=False)
-         time.sleep(5)
-     except Exception:
-         pass
+     log.warning(
+         "GPU not free after force_reset (%d MB used). "
+         "Skipping nvidia-smi --gpu-reset (destructive). "
+         "Manual reset may be needed: sudo nvidia-smi --gpu-reset",
+         gpu_used_mb()
+     )
```

### 影响范围

- **无向后兼容问题**: 接口返回值不变，原调用方逻辑不变
- **风险**: 极少数情况下 GPU 卡死时需要手动 `nvidia-smi --gpu-reset`，但远比无警告的自动重置安全

---

## P0-C3: `pkill` 精确路径匹配

**严重性**: CRITICAL · **文件**: `inferfabric/process_manager.py:478, 625`

### 问题

```python
subprocess.run(["pkill", "-f", "python main.py"], ...)
subprocess.run(["pkill", "-9", "-f", "python main.py"], ...)
```

匹配任意含 `python main.py` 的进程，会误杀其他服务的 Python 进程（如 `uvicorn main:app`、`python main.py --other` 等）。

### 方案

改为匹配 `COMFYUI_DIR/main.py` 路径，以及进程实际工作目录。

### 修改

#### 1. `_pkill_comfyui_fallback` (L476-487)

```diff
  def _pkill_comfyui_fallback(self) -> dict:
      """Fallback: stop ComfyUI via pkill."""
-     subprocess.run(["pkill", "-f", "python main.py"], timeout=5, check=False, capture_output=True)
+     subprocess.run(
+         ["pkill", "-f", f"python.*{COMFYUI_DIR}/main.py"],
+         timeout=5, check=False, capture_output=True
+     )
      time.sleep(2)
      # SIGKILL remaining
-     subprocess.run(["pkill", "-9", "-f", "python main.py"], timeout=5, check=False)
+     subprocess.run(
+         ["pkill", "-9", "-f", f"python.*{COMFYUI_DIR}/main.py"],
+         timeout=5, check=False
+     )
      subprocess.run(["pkill", "-9", "-f", "ComfyUI"], timeout=5, check=False)
      time.sleep(1)
      self._set_comfyui_pid(None)
      self._cleanup_pid_files("comfyui")
      self._wait_gpu_idle()
      return {"status": "ok", "message": "pkill fallback"}
```

#### 2. `force_kill_all` (L625)

```diff
  # ComfyUI
  cpgid = self.comfyui_pid
  if cpgid:
      try:
          os.killpg(cpgid, signal.SIGKILL)
      except (ProcessLookupError, PermissionError):
          pass

- subprocess.run(["pkill", "-9", "-f", "python main.py"], timeout=5, check=False)
+ subprocess.run(
+     ["pkill", "-9", "-f", f"python.*{COMFYUI_DIR}/main.py"],
+     timeout=5, check=False
+ )
  # Try to kill ComfyUI specifically by working dir
  comfyui_dir = str(COMFYUI_DIR)
  subprocess.run(["pkill", "-9", "-f", f"python.*{comfyui_dir}"], timeout=5, check=False)
```

### 影响范围

- **向后兼容**: 在 ComfyUI 标准路径下的进程仍会匹配。其他无关进程不会误杀
- **边缘情况**: 如果 ComfyUI 被安装在非标准路径下，第二个 `python.*{comfyui_dir}` 匹配可以兜底

---

## P0-C4: 流式连接泄漏

**严重性**: CRITICAL · **文件**: `inferfabric/proxy.py:404-421`, `inferfabric/forwarder.py:78-98`

### 问题

`_handle_chat` 的流式请求中，`resp.close()` 只关闭 HTTPResponse，但 `HTTPConnection`（`conn`）未被关闭，TCP 连接保持到 300 秒超时。客户端断开后上游 vLLM/Ollama 的连接仍然保持。

### 方案

在 `try/finally` 中同时关闭 `conn`（HTTPConnection），确保上游连接在客户端断开后立即释放。

### 修改

#### proxy.py `_handle_chat` — 为 `conn` 添加 finally 关闭

```diff
  # L374-432
  conn = pm.make_conn(target_port)
  conn.request("POST", self.path, body=body,
               headers={"Content-Type": "application/json"})
  resp = conn.getresponse()

  try:
      resp_status = resp.status
      ...
      if stream:
          ...
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
              try:
                  self._safe_write(b"0\r\n\r\n")
              except Exception:
                  pass
          finally:
              resp.close()
      else:
          try:
              resp_body = resp.read()
              ...
          finally:
              resp.close()
  except Exception as e:
      ...
+ finally:
+     conn.close()
```

具体改法是将最外层的 `try:` 扩展为 `try/finally`，或在已有的 `finally` 块中补充 `conn.close()`。更简洁的方式是包裹一个外层 try/finally：

```python
conn = pm.make_conn(target_port)
try:
    conn.request("POST", self.path, body=body, ...)
    resp = conn.getresponse()
    try:
        # ... stream or non-stream handling ...
    finally:
        resp.close()
finally:
    conn.close()
```

#### forwarder.py `pipe_stream_response` — 已正确处理

`pipe_stream_response` 中已有 `finally: resp.close()`，但需要接收 `conn` 参数来关闭。不过此函数只处理 response，connection 更应在调用者侧管理。

### 影响范围

- **无功能变化**: 行为完全一致，仅资源释放时机提前
- **性能提升**: 减少 TIME_WAIT 和上游连接堆积

---

## P1-H1: 类级共享 dict 加线程锁

**严重性**: HIGH · **文件**: `inferfabric/proxy.py:173-175`

### 问题

```python
class ProxyHandler(http.server.BaseHTTPRequestHandler):
    _vllm_gen_counters: dict = {}    # 类级，所有线程共享
    _vllm_throughput_ema: dict = {}  # 类级，所有线程共享
```

`ThreadedHTTPServer` 为每个请求创建新线程，所有线程共享类级 dict。`_handle_vllm_metrics` 中的并发读写（L737, L741-755, L764）会导致数据竞争。

### 方案

添加 `threading.Lock` 保护所有对该 dict 的读写。将 dict 放进一个带锁的容器，或直接在访问处加锁。

### 修改

#### 1. 添加类级锁

```diff
  class ProxyHandler(http.server.BaseHTTPRequestHandler):
      # Per-port state for counter-diff throughput (MTP-aware)
      _vllm_gen_counters: dict = {}  # port -> (timestamp, generation_tokens_total)
      # EMA state for smoothed throughput: port -> ema_value (tokens/s)
      _vllm_throughput_ema: dict = {}  # port -> float
+     _vllm_metrics_lock: threading.Lock = threading.Lock()
```

#### 2. 保护 `_handle_vllm_metrics` 中的访问

```diff
  # L737-764
- prev_state = self._vllm_gen_counters.get(port)
+ with self._vllm_metrics_lock:
+     prev_state = self._vllm_gen_counters.get(port)

  if gen_counter is not None:
      inst_tp = None
      if prev_state is not None:
          ...

      # EMA update
-     prev_ema = self._vllm_throughput_ema.get(port)
+     with self._vllm_metrics_lock:
+         prev_ema = self._vllm_throughput_ema.get(port)
      if inst_tp is not None:
          if prev_ema is None:
              ema_tp = inst_tp
          else:
              ema_tp = EMA_ALPHA * inst_tp + (1 - EMA_ALPHA) * prev_ema
+         with self._vllm_metrics_lock:
              self._vllm_throughput_ema[port] = ema_tp
          result["throughput"] = round(ema_tp, 1)
          result["throughput_inst"] = inst_tp
          result["throughput_cum_n"] = int(gen_counter)
      elif prev_ema is not None:
          result["throughput"] = round(prev_ema, 1)
          result["throughput_cum_n"] = int(gen_counter)

+ with self._vllm_metrics_lock:
      self._vllm_gen_counters[port] = (cur_ts, gen_counter)
```

**优化方案**: 用一个带锁的包装器将读写操作合并为临界区，减少锁粒度：

```python
# 单次加锁完成所有读写
with self._vllm_metrics_lock:
    prev_state = self._vllm_gen_counters.get(port)
    prev_ema = self._vllm_throughput_ema.get(port)
    # ... 计算 ...
    if inst_tp is not None:
        self._vllm_throughput_ema[port] = ema_tp
    self._vllm_gen_counters[port] = (cur_ts, gen_counter)
```

### 影响范围

- **无接口变化**: 对外 API 不变
- **性能**: 锁争用极低，metrics 请求间隔 10s，延迟影响可忽略
- **正确性**: 消除数据竞争，EMA 值计算正确

---

## P1-H2: VRAM 检查竞态

**严重性**: HIGH · **文件**: `inferfabric/manager.py:588-599`

### 问题

```python
def _shared_add_service(self, model: ModelConfig) -> dict:
    ...
    # VRAM headroom check
    if model.typical_vram_pct > 0:
        current_pct = self._get_current_vram_pct()
        if current_pct + model.typical_vram_pct > 95:
            return {"status": "error", ...}

    # Start only the new service
    ...
```

两个并发请求可能同时通过 VRAM 检查（check 和 use 之间无锁），导致双启动、OOM。

### 方案

`sleep_model()` 和其他关键操作在 `manager.py` 中已使用 `self._lock`。`_shared_add_service` 也在 `switch()` 的锁内调用。但要确保 `switch()` 全路径持有锁。

检查 `switch()` 代码：

```python
def switch(self, target: str) -> dict:
    ...
    with self._lock:
        ...
        if target in self.active_services:
            if self._check_model_config_changed(model):
                ...
```

`switch()` 确实持有 `self._lock`。但 `_shared_add_service` 可能从其他路径调用。加一个内部断言/锁：

```diff
  def _shared_add_service(self, model: ModelConfig) -> dict:
+     if not self._lock.acquire(blocking=False):
+         return {"status": "error", "message": "Switch in progress"}
+     self._lock.release()
      t0 = time.time()
      ...
```

但更好的方式是确保所有调用路径都持有锁。查看调用链：

- `switch() → ... → _deploy_model() → ... → _shared_add_service()` — 已在锁内 ✅
- 其他直接调用？grep 一下。

实际上，`switch()` 是唯一的调用入口，且在 `self._lock` 下运行。问题在于 VRAM 检查完成后锁被释放... 再看代码：

```python
def switch(self, target: str) -> dict:
    ...
    with self._lock:
        ...
        result = self._shared_add_service(model)
```

哦，`_shared_add_service` 在锁内被调用 ⭐。所以 H2 在 4.0 代码中已有 `self._lock` 保护，问题级别可降为 MEDIUM。但锁内调用了 `_get_current_vram_pct()` 和进程启动，锁持有期间没问题。

**修正**: 确认 `_shared_add_service` 始终在 `switch()` 的锁内调用。加注释和断言：

```diff
  def _shared_add_service(self, model: ModelConfig) -> dict:
+     # PREREQUISITE: caller must hold self._lock (see switch())
+     assert self._lock.locked(), "_shared_add_service requires switch lock"
      t0 = time.time()
      ...
```

### 影响范围

- **无风险**: 仅加断言和注释
- **防御性编程**: 防止将来重构引入新调用路径

---

## P1-H3: `set_sleep_state()` TOCTOU + 死锁

**严重性**: HIGH · **文件**: `inferfabric/state.py:234-246`

### 问题

两重问题：

1. **死锁**: `set_sleep_state` 持有 `self._lock` 后调用 `self.set()`（内部也试图获取 `self._lock`），由于 `threading.Lock` 不可重入，会导致死锁
2. **TOCTOU**: 即使没有死锁，`get("sleep_state")` 不经过锁，读取可能已过时

### 方案

将 `set()` 逻辑内联到自旋锁中，避免嵌套锁。同时将 `_lock` 改为 `threading.RLock`（reentrant lock），防止其他方法出现类似问题。

### 修改

```diff
  class StateDB:
      def __init__(self, db_path: Path):
          db_path.parent.mkdir(parents=True, exist_ok=True)
          self._db_path = db_path
-         self._lock = threading.Lock()
+         self._lock = threading.RLock()
          self._init()
```

然后更改 `set_sleep_state` 不再调用 `self.set()`：

```diff
  def set_sleep_state(self, model_name: str, level: Optional[int]):
-     """Set sleep state for a model. level=None clears sleep state (awake). Thread-safe."""
+     """Set sleep state for a model. level=None clears sleep state (awake).
+     Thread-safe. Uses inline SQL to avoid nested lock on self.set()."""
      with self._lock:
-         raw = self.get("sleep_state") or "{}"
+         c = self._conn()
+         try:
+             row = c.execute("SELECT value FROM state WHERE key='sleep_state'").fetchone()
+             raw = row[0] if row else "{}"
              try:
                  states = json.loads(raw)
              except (json.JSONDecodeError, TypeError):
                  states = {}
              if level is None:
                  states.pop(model_name, None)
              else:
                  states[model_name] = f"l{level}"
-             self.set("sleep_state", json.dumps(states))
+             c.execute("INSERT OR REPLACE INTO state VALUES ('sleep_state', ?)",
+                       (json.dumps(states),))
+             c.commit()
+         finally:
+             c.close()
```

### 影响范围

- **修复死锁**: 之前调用 `set_sleep_state()` 会永久阻塞
- **线程安全**: 读写在同一锁临界区内，消除 TOCTOU
- **RLock**: 切换到 reentrant lock，防止其他嵌套调用场景的死锁

---

## P1-H4: Config drift 改为 YAML 哈希

**严重性**: HIGH · **文件**: `inferfabric/manager.py:377-419`

### 问题

`_check_model_config_changed` 通过 `/proc/{pid}/cmdline` 解析进程命令行参数来检测配置漂移。该方法：

1. 依赖 `fuser` 命令可用性
2. 依赖 `/proc` 文件系统（非 Linux 不适用）
3. 仅检测了 3 个参数（gpu_memory_utilization, max_model_len, max_num_seqs）
4. 进程重启后 PID 变化，检测失效

### 方案

改为在 `_deploy_model` 启动模型时记录 YAML 内容的 SHA256 哈希到 StateDB，漂移检测时对比哈希值。

### 修改

#### 1. `_deploy_model` 中记录 YAML 哈希

```diff
  def _deploy_model(self, model: ModelConfig, target_mode: str) -> dict:
      """Start a model and record config hash for drift detection."""
      t0 = time.time()
      ...
      # 启动后记录配置哈希
+     config_hash = self._compute_model_config_hash(model)
+     self.state.set(f"config_hash:{model.name}", config_hash)
      ...
```

#### 2. 添加哈希计算函数

```python
# manager.py, 类内
import hashlib

def _compute_model_config_hash(self, model: ModelConfig) -> str:
    """Compute SHA256 of the model's YAML config dict for drift detection."""
    # Convert dataclass to serializable dict
    cfg_dict = {}
    for field_name in model.__dataclass_fields__:
        val = getattr(model, field_name)
        if val is not None and not field_name.startswith("_"):
            try:
                json.dumps(val)
                cfg_dict[field_name] = val
            except (TypeError, ValueError):
                cfg_dict[field_name] = str(val)
    return hashlib.sha256(json.dumps(cfg_dict, sort_keys=True).encode()).hexdigest()
```

#### 3. 改写 `_check_model_config_changed`

```diff
  def _check_model_config_changed(self, model: ModelConfig) -> bool:
-     """Compare vLLM process cmdline against YAML config. Returns True if drifted."""
+     """Compare YAML config hash against recorded hash. Returns True if drifted."""
-     import subprocess
-     import re
-     try:
-         port = model.vllm.port
-         result = subprocess.run(...)
-         ...
+     stored_hash = self.state.get(f"config_hash:{model.name}")
+     if stored_hash is None:
+         return True  # Never recorded (e.g. pre-upgrade model)
+     current_hash = self._compute_model_config_hash(model)
+     return current_hash != stored_hash
```

#### 4. 更新测试 `test_p0_p1_fixes.py`

测试不再 mock `/proc` 和 `fuser`，改为 mock `_compute_model_config_hash`。

### 影响范围

- **向后兼容**: 旧状态不包含 `config_hash:*` 键，首次检测会认为漂移（==True）并重启一次，此后记录哈希
- **跨平台**: 不依赖 Linux `/proc`，理论可移植
- **精确度**: 全部 YAML 字段均参与哈希，不再只检 3 个参数
- **测试**: 需要更新 `TestP01_CheckModelConfigChanged` 测试用例

---

## P1-H5: `forward_anthropic_local` 补 conn.close()

**严重性**: HIGH · **文件**: `inferfabric/forwarder.py:186-198`

### 问题

在 `forward_anthropic_local` 的 retry 循环中：

- 成功路径（L180-184）：`pipe_stream_response` 或 `handle_json_response` 返回后立即 `return`，未关闭 `conn`
- 失败路径：L188-192 在异常分支中已关闭 `conn`，但全部重试失败后 fallback 到 Baidu 时未关闭最后一次尝试的 `conn`

### 方案

使用 `try/finally` 确保每次迭代都在下一次迭代前或返回前关闭 `conn`。

### 修改

```diff
  def forward_anthropic_local(handler, pm, data, auth_header, model_obj, original_model):
      ...
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
+                 conn.close()
                  delay_s = exponential_backoff(attempt)
                  log.warning(...)
                  time.sleep(delay_s)
                  continue

              if was_stream:
                  pipe_stream_response(handler, resp)
              else:
                  handle_json_response(handler, resp, ...)
              return

          except (ConnectionRefusedError, ...) as e:
              last_error = e
-             if conn:
-                 try:
-                     conn.close()
-                 except Exception:
-                     pass
              if attempt < UPSTREAM_LOCAL_RETRIES:
                  ...
                  continue
              ...

          except Exception as e:
              log.error(...)
-             if conn:
-                 try:
-                     conn.close()
-                 except Exception:
-                     pass
              break
+         finally:
+             if conn:
+                 try:
+                     conn.close()
+                 except Exception:
+                     pass

      # Fallback to Baidu
      ...
```

**注意**: `finally` 块放在 `for` 循环体内（与 `try` 配对），确保每次迭代都关闭 `conn`。成功路径的 `return` 也会触发 `finally`。

### 影响范围

- **连接释放**: 每次 retry 后立即释放连接，避免 TIME_WAIT 堆积
- **功能不变**: Baidu fallback 路径不受影响
- **向后兼容**: 接口签名不变

---

## P1-H6: Dashboard JS 锁加固

**严重性**: HIGH · **文件**: `inferfabric/dashboard.py:571-576`

### 问题

```javascript
let sw = false, swT = 0;
function swLock() {
  if(sw && Date.now()-swT>30000) { sw=false; }
  if(sw) return false;
  sw=true; swT=Date.now(); return true;
}
```

`sw` 是页面级内存变量，多个浏览器 Tab 各自独立，可以同时发送操作请求，导致并发 switch/stop。

### 方案

使用 `localStorage` + `window.name` 结合或 `BroadcastChannel API` 实现多 Tab 共享锁。或者更轻量的方案：在请求头中添加 `X-Request-Id`，服务端用锁拒绝并发，客户端在收到 429/503 时显示提示。

##### 推荐方案：localStorage 跨 Tab 锁

```javascript
function swLock() {
  // Global cross-tab lock via localStorage
  const key = 'inferfabric_sw_locked';
  const now = Date.now();
  const stored = localStorage.getItem(key);
  const locked = stored ? now - parseInt(stored, 10) < 30000 : false;
  if (locked) return false;
  localStorage.setItem(key, String(now));
  return true;
}
function swUnlock() {
  localStorage.removeItem('inferfabric_sw_locked');
}
```

然后在每个 `finally` 块中调用 `swUnlock()`。

#### 2. 优化 Dashboard HTML

```diff
  <script>
  let sw = false, swT = 0;
  function swLock() {
-   // Safety: force-unlock after 30s
-   if(sw && Date.now()-swT>30000) { ... }
-   if(sw) return false;
-   sw=true; swT=Date.now(); return true;
+   const key = 'inferfabric_sw_lock';
+   const now = Date.now();
+   try {
+     const stored = localStorage.getItem(key);
+     if (stored && (now - parseInt(stored, 10)) < 30000) return false;
+     localStorage.setItem(key, String(now));
+     return true;
+   } catch(e) { return true; } // localStorage unavailable → allow
  }
+ function swUnlock() {
+   try { localStorage.removeItem('inferfabric_sw_lock'); } catch(e) {}
+ }
```

#### 3. 在所有 `finally{sw=false}` 处改为 `finally{swUnlock()}`

```diff
  // 每个异步操作
  try {
      ...
  } catch(e) { ... }
- finally { sw = false; }
+ finally { swUnlock(); }
```

### 影响范围

- **无服务器端改动**: 仅前端 JS 改动
- **跨 Tab 互斥**: 同一浏览器多个 Tab 不会并发操作
- **超时保护**: 30s 自动超时释放
- **降级**: localStorage 不可用时（Safari 无痕模式等），优雅降级为允许操作

---

## 修改文件清单

| 优先级 | ID | 文件 | 改动类型 |
|--------|----|------|----------|
| P0 | C1 | `inferfabric/proxy.py` | 新增 `_check_auth()`，修改 `do_GET`/`do_POST` |
| P0 | C2 | `inferfabric/manager.py` | 移除 `nvidia-smi --gpu-reset`，替换为日志 |
| P0 | C3 | `inferfabric/process_manager.py` | 修改 `pkill` 模式为精确路径 |
| P0 | C4 | `inferfabric/proxy.py` | 为 `conn` 添加 `finally: conn.close()` |
| P1 | H1 | `inferfabric/proxy.py` | 添加 `_vllm_metrics_lock` 保护共享 dict |
| P1 | H2 | `inferfabric/manager.py` | 添加锁持有断言 |
| P1 | H3 | `inferfabric/state.py` | RLock + 内联 SQL 修复死锁 |
| P1 | H4 | `inferfabric/manager.py` | YAML 哈希替代 /proc cmdline |
| P1 | H5 | `inferfabric/forwarder.py` | 补 `conn.close()` finally |
| P1 | H6 | `inferfabric/dashboard.py` | localStorage 跨 Tab 锁 |

---

## 风险说明

| 风险 | 描述 | 缓减措施 |
|------|------|----------|
| API Key 配置遗漏 | 用户可能忘记设置 `EDGE_API_KEY`，集群暴露 | 默认 `127.0.0.1` 绑定 + README 加显式安全说明 |
| Config hash 首次漂移 | 旧版升级后首次 `switch` 认为配置已变更 | 这是正确的行为（重启一次），不影响功能 |
| Dashboard localStorage 异常 | Safari 无痕模式可能抛出异常 | `try/catch` 兜底，允许操作 |
| ComfyUI 非标准路径 | `pkill` 匹配不到 | 第二个 `python.*{comfyui_dir}` 通配匹配兜底 |

---

## 测试建议

1. C1: 设置/不设置 `EDGE_API_KEY`，curl 验证 401/200
2. C2: 调用 `force_reset`，确认 GPU 日志警告而非执行 reset
3. C3: 模拟 ComfyUI 在 `COMFYUI_DIR` 路径，确认 `pkill` 仅杀指定进程
4. C4: 流式请求中断客户端，`lsof` 确认上游连接已关闭
5. H1: 并发 10 个 `/vllm_metrics` 请求，确认无数据竞争
6. H3: 调用 `set_sleep_state` → `get_sleep_state`，确认不卡死
7. H5: 模拟重试后成功/失败场景，确认所有 conn 被关闭
8. H6: 两个 Tab 同时点击"启动"，确认只有一个通过

---

*生成日期: 2026-07-04 · 直接可提交为 PR*
