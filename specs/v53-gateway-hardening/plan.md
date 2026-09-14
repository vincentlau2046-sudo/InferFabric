# v53 实施计划（全面版）

> 对应需求：见同目录 `spec.md`。
> 审核通过后按 **R8→R9→R10→R1→R2→R3→R4→R5→R6→R7** 顺序实施。
> 架构硬约束：透明网关（不翻译协议）；模型自动切换是 Agent 职责，IFF 不做 fallback；**零新增 Python 依赖**（当前唯一第三方依赖是 PyYAML）。

---

## 依赖基线

- Python 3.12+（stdlib 三件套：`http.server`、`urllib.request`、`threading`）
- PyYAML（唯一非标准库依赖）
- formatting: `black` + `isort`（仅格式化，非运行时依赖）

---

## R8. 🔴 削除 Step 6/7 静默 fallback（最高优先级）

### 8.1 现状分析

`_handle_messages`（`handler.py:413-458`）在 `resolve_route`（Step 5）未命中后，有两个兜底机制：

**Step 6 — "fallback to first active LLM"**（line 413-425）

```python
active_llm = None
for svc in pm.mgr.active_services:
    model_obj = pm.mgr.get_model(svc)
    if model_obj and model_obj.model_type in forwarder.LOCAL_LLM_TYPES:
        if model_obj.port:
            active_llm = model_obj
            break

if active_llm:
    log.info("... [fallback: no model match for %s]", requested_model)
    self._forward_local(pm, data, auth_header, active_llm, original_model)
```

客户端传 `model=xyz-v42`，IFF 不认识，但 qwen38 正在跑 → 把请求转发到 qwen38:8002。
vLLM 收到不认识的 `model=xyz-v42`：可能报错、可能默认回复、可能输出与预期完全无关的内容。

**Step 7 — "cloud fallback"**（line 427-458）

```python
else:  # active_llm is None
    # 遍历所有 cloud_models，用 [{provider_name}/{short_name}, short_name] 硬找
    cloud_model = find_any_cloud_match(short_name)
    if cloud_model:
        forward_to_cloud(data, cloud_model)
```

本地完全不能用 → 在云端随便找一个模型发过去。云端收到 `model=xyz-v42`：可能报错、可能被扣费、可能产出乱码。

### 8.2 两个 fallback 何时被触发

Step 6 和 Step 7 的触发路径：

```
请求 model=XYZ
→ find_model_by_served_name(XYZ) → None（本地无此 served_name）
→ resolve_route(XYZ, local_models) → None（云端也不认识）
→ ↓ 到达 Step 6

Step 6: active_services 里有 LLM 类型
  → YES → _forward_local(qwen38)  ← 静默转发
  → NO  → Step 7

Step 7: cloud_models 里有 XYZ 的某种匹配
  → YES → forward_to_cloud(baidu)  ← 静默转发
  → NO  → 503 "no route available"
```

`resolve_route`（Step 5）已经用 `cloud_models.get(short_name) or cloud_models.get(model_name)` 做过精确匹配。Step 7 的遍历 `[{provider}/{short_name}, short_name]` 查的是同一份 `cloud_models` 字典——**功能完全冗余**。

`resolve_route` 的 `cloud_models` 存储格式（`cloud_discovery.py:306-308`）：
```python
merged[m.model_id] = m                    # "deepseek-v4-flash" → CloudModel
merged[f"{m.provider}/{m.model_id}"] = m  # "baidu-codingplan/deepseek-v4-flash" → CloudModel
```

`resolve_route`（line 352）：
```python
cm = self._cloud_models.get(short_name) or self._cloud_models.get(model_name)
```

所以 Step 7 的额外搜索 {provider_name}/{short_name} 最多补充 `resolve_route` 没有的 provider-prefixed 查找——但 `cloud_models` 已经存了 provider-prefixed key。

**结论**：Step 6 和 Step 7 在 `resolve_route` 出现后均已功能冗余，且违反"不静默 fallback"的架构红线。

### 8.3 当前流量分析

在日常使用中，这两个 fallback 的触发概率：

| 请求 model | 本地匹配 | resolve_route | 触发 Step 6/7？ |
|-----------|---------|--------------|----------------|
| `haiku` | ❌ | `cloud:baidu` ✅ | **不会** |
| `deepseek-v4-flash`（qwen38 active） | ✅ local | 不走云路由 | **不会** |
| `deepseek-v4-flash`（qwen38 not active） | ✅ local（auto-switch） | 不走云路由 | **不会** |
| `xyz-not-exist` | ❌ | ❌ None | **会** → 改为 404 |

### 8.4 修正方案

削除 Step 6 和 Step 7，合并为统一拒绝逻辑：

```python
# Before (两个相续的 if/else 块，约 45 行)
if active_llm:
    _forward_local(data, active_llm)   # 静默 fallback
    return
else:
    cloud_model = find_any_match(...)
    if cloud_model:
        forward_to_cloud(data, cloud_model)  # 静默 fallback
        return
    _send_json({"error": "No active local model and no cloud route"}, 503)

# After (统一为 ~10 行)
log.warning("/v1/messages → rejecting unknown model: %s", requested_model)
pm.anomalies.record(AnomalyEvent(
    category="routing", severity="warning", model=requested_model,
    status_code=404,
    message=f"Unknown model '{requested_model}' — rejected: no match in local served_names or cloud_models",
    possible_cause="1. 客户端传了不存在的模型名；2. cloud_provider.yaml 缺少对应 model_id；"
                   "3. models.d/*.yaml 的 served_name 配置未覆盖此模型名",
))
_send_json({"error": f"Unknown model: {requested_model}"}, 404)
```

**行为变更**：

| 场景 | 当前（静默 fallback） | R8 后（明确拒绝） |
|------|-------------------|----------------|
| agent 传错模型名 | 收到 vLLM/云端的不确定输出 | 收到明确 404 + 原因 |
| cloud_provider.yaml 配置遗漏 | 可能走到错误模型 | 立即触发异常记录，运维可见 |
| 正常请求（有匹配） | 不受影响 | 不受影响 |

### 8.5 涉及修改

- `inferfabric/proxy/handler.py`：`_handle_messages` 底部第 413-458 行，两个 fallback 块合并为统一拒绝（~45 行→~10 行）。
- 依赖 R9 `AnomalyCollector`（record 调用需要 `pm.anomalies`）。

### 8.6 边界情况

| 场景 | 行为 |
|------|------|
| 请求 model=deepseek-v4-flash，qwen38 active | 不受影响（本地路由 line 337 返回） |
| 请求 model=haiku，baidu 已注册 | 不受影响（云路由 line 386 返回） |
| qwen38 OOM 后请求 model=deepseek-v4-flash | auto-switch → switch 失败 → 503 + Retry-After: 10（R0） |
| 请求 model=xyz-v42 | **R8 新行为** → 404 + anomaly record |
| active_services 为空 + 请求 model=haiku | 云路由 step 5 命中 → 正常返回（不受 R8 影响） |

### 8.7 测试

```python
# 发送 model=xyz-v42 到 /v1/messages → 断言 404 + Retry-After 不存在（非 R0 场景）
# 发送 model=xyz-v42 到 /v1/chat/completions → 断言 404（OpenAI 路径原本就是 404）
# 断言 /api/anomalies 返回 1 条 category=routing, severity=warning 的事件
# 发送正常 model=haiku → 断言 200（云路由不受影响）
```

### 8.8 风险评估

**风险等级：极低。** 削除两个在正常流量下不会被触发的冗余兜底逻辑。变更集中在 `_handle_messages` 底部 45 行，不改变任何路由判定逻辑（Step 4 本地路由 + Step 5 云路由在 Step 6/7 之前已返回或 fall through；Step 6/7 是最后一个 fallback stage）。

---

## R9. 🟡 AnomalyCollector 基础设施

### 9.1 设计目标

- 采集 IFF 内部的结构化异常事件（未知模型、switch 失败、配置缺失等）。
- 通过 API 暴露给 dashboard 或外部监控。
- 轻量：纯内存环形缓冲 + 线程安全锁。**不持久化**（异常是瞬时快照，不是审计日志）。
- 零新依赖。

### 9.2 数据结构

```python
# anomaly_collector.py

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AnomalyEvent:
    id: str                    # hex(ts) + "-" + 4-hex-counter
    ts: float                  # time.time()
    category: str              # "routing" | "model" | "auth" | "config" | "cloud"
    severity: str              # "info" | "warning" | "error" | "critical"
    model: str                 # 请求中的模型名（或 "" 若非请求触发）
    message: str               # 人类可读描述
    status_code: int           # HTTP 状态码（0 若非 HTTP 场景）
    possible_cause: str        # 面向运维的原因
    detail: Optional[dict] = None


class AnomalyCollector:
    """Thread-safe ring buffer for anomaly events."""

    def __init__(self, maxsize: int = 500):
        self._maxsize = maxsize
        self._events: list[AnomalyEvent] = []
        self._counter = 0
        self._lock = threading.Lock()

    def record(self, event: AnomalyEvent) -> None:
        with self._lock:
            # 事件 ID = hex 时间戳 + 递进计数器
            event.id = f"{int(event.ts * 1000):x}-{self._counter:04x}"
            self._counter += 1
            self._events.append(event)
            if len(self._events) > self._maxsize:
                self._events.pop(0)  # 淘汰最旧事件

    def query(self, since: float = 0, limit: int = 100,
              category: str | None = None,
              severity: str | None = None) -> list[AnomalyEvent]:
        with self._lock:
            result = [e for e in self._events if e.ts >= since]
            if category:
                result = [e for e in result if e.category == category]
            if severity:
                result = [e for e in result if e.severity == severity]
            result.sort(key=lambda e: e.ts, reverse=True)
            return result[:limit]

    def count(self) -> int:
        with self._lock:
            return len(self._events)
```

### 9.3 与现有系统融合

**初始化**：在 `ProxyManager.__init__`（`proxy_manager.py:48-98`）末尾添加：

```python
from inferfabric.anomaly_collector import AnomalyCollector
self.anomalies = AnomalyCollector()
```

**API 端点**：在 `_GET_ROUTES`（`handler.py:65-88`）中新增：

```python
"/api/anomalies": lambda h, pm: h._handle_anomalies(pm),
```

**handler 方法**：

```python
def _handle_anomalies(self, pm):
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(self.path).query)
    since = float(qs.get("since", ["0"])[0])
    limit = min(int(qs.get("limit", ["100"])[0]), 500)
    category = qs.get("category", [None])[0]
    severity = qs.get("severity", [None])[0]
    events = pm.anomalies.query(since=since, limit=limit,
                                 category=category, severity=severity)
    self._send_json({
        "events": [{
            "id": e.id,
            "ts": e.ts,
            "category": e.category,
            "severity": e.severity,
            "model": e.model,
            "message": e.message,
            "status_code": e.status_code,
            "possible_cause": e.possible_cause,
            "detail": e.detail,
        } for e in events],
        "count": len(events),
    })
```

### 9.4 采集点注入（与 Phase 1 其他需求共享）

每个早退路径同时做三件事：
1. 写 `RequestLog`（R1）。
2. record `AnomalyEvent`（R9）。
3. 返回错误（R0/R8 等）。

具体的采集点清单见 `spec.md` 附录 B 的"各采集点的 Anomaly 事件映射"表。

### 9.5 涉及修改

| 文件 | 操作 | 行数估算 |
|------|------|---------|
| 新增 `inferfabric/anomaly_collector.py` | 新建 | ~70 行 |
| `inferfabric/proxy_manager.py` | `__init__` 末尾 + `self.anomalies = AnomalyCollector()` | 3 行 |
| `inferfabric/proxy/handler.py` | 新增 `_handle_anomalies` + `_GET_ROUTES` 注册 | 30 行 |
| `inferfabric/proxy/handler.py` + `chat_handlers.py` | 各采集点注入 `pm.anomalies.record(...)` | 各 1 行/点 |

### 9.6 测试

```python
# 单元测试：record 1 条 → query 返回 1 条，count==1
# 单元测试：record 501 条 → query 返回 500 条（最旧 1 条被淘汰）
# 单元测试：query(category="routing") → 只返回 routing 事件
# 单元测试：query(severity="error") → 只返回 error 事件
# 集成测试：/api/anomalies 返回 JSON，events 字段存在
```

### 9.7 风险评估

**风险等级：极低。** 纯数据采集 + API 暴露，不修改任何请求处理逻辑。`AnomalyCollector` 是独立新增类，与现有系统无耦合。

---

## R10. 🟡 异常看板 dashboard tab

### 10.1 现状

IFF dashboard 是 `base.html` + 5 个 HTML fragment 组装 (`dashboard/__init__.py`)。每个 tab 对应一个 fragment 文件和一个 JS loader。

```
fragments/overview.html   → 总览
fragments/inference.html  → 推理
fragments/monitor.html    → 监控
fragments/deploy.html     → 部署
fragments/cloud.html      → 云端
```

JS 负责轮询 API 数据并渲染；现有 `app.js` 通过 `window.store.on('tab_active', ...)` 绑定 tab 切换事件。

### 10.2 新增 fragment

**`dashboard/fragments/anomaly.html`**：

```html
<div class="tab-content" id="tab-anomaly" style="padding: 1.5rem">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
    <h2 style="font-size:1.25rem;font-weight:600;color:var(--text1)">⚠️ 异常事件</h2>
    <div style="display:flex;gap:8px;align-items:center">
      <label style="color:var(--text3);font-size:13px">类别</label>
      <select id="anomaly-category-filter" class="if-select" style="padding:4px 8px;border:1px solid var(--bg3);border-radius:6px;background:var(--bg1);color:var(--text1)">
        <option value="">全部</option>
        <option value="routing">路由</option>
        <option value="model">模型</option>
        <option value="auth">认证</option>
        <option value="config">配置</option>
        <option value="cloud">云端</option>
      </select>
      <label style="color:var(--text3);font-size:13px">严重度</label>
      <select id="anomaly-severity-filter" class="if-select" style="padding:4px 8px;border:1px solid var(--bg3);border-radius:6px;background:var(--bg1);color:var(--text1)">
        <option value="">全部</option>
        <option value="info">🟢 info</option>
        <option value="warning">🟡 warning</option>
        <option value="error">🔴 error</option>
        <option value="critical">⚫ critical</option>
      </select>
    </div>
  </div>
  <div id="anomaly-event-list" style="font-family:var(--mono);font-size:13px">
    <p style="color:var(--text3)">加载中...</p>
  </div>
</div>
```

**`dashboard/js/anomaly.js`**：

```javascript
(function() {
  const POLL_INTERVAL = 10000; // 10s
  let timer = null;
  let eventsHtml = '';

  function renderSeverityBadge(s) {
    const m = {info:'🔵 info',warning:'🟡 warning',error:'🔴 error',critical:'⚫ critical'};
    return m[s]||s;
  }

  function renderEvent(e) {
    const d = new Date(e.ts*1000);
    const time = d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    return `<div style="display:flex;gap:12px;padding:10px 0;border-bottom:1px solid var(--bg3);align-items:flex-start">
      <span style="color:var(--text3);white-space:nowrap;min-width:60px">${time}</span>
      <span style="white-space:nowrap;min-width:40px;font-weight:600">${e.status_code}</span>
      <span style="min-width:120px;color:var(--text2)">${e.model||'<empty>'}</span>
      <span style="min-width:100px">${renderSeverityBadge(e.severity)}</span>
      <span style="flex:1;color:var(--text1)">${e.message}</span>
    </div>
    <div style="padding:0 0 10px 72px;font-size:12px;color:var(--text3)">${e.possible_cause}</div>`;
  }

  async function loadAnomalies() {
    const cat = document.getElementById('anomaly-category-filter').value;
    const sev = document.getElementById('anomaly-severity-filter').value;
    let url = '/api/anomalies?limit=200';
    if (cat) url += '&category=' + cat;
    if (sev) url += '&severity=' + sev;
    try {
      const resp = await fetch(url);
      const data = await resp.json();
      const list = document.getElementById('anomaly-event-list');
      if (!data.events || data.events.length === 0) {
        list.innerHTML = '<p style="color:var(--text3)">暂无异常事件</p>';
        return;
      }
      list.innerHTML = data.events.map(renderEvent).join('');
    } catch(e) {
      document.getElementById('anomaly-event-list').innerHTML =
        '<p style="color:var(--text3)">加载异常数据失败: ' + e.message + '</p>';
    }
  }

  window.store.on('tab_active', (val) => {
    if (val === 'tab-anomaly') {
      loadAnomalies();
      timer = setInterval(loadAnomalies, POLL_INTERVAL);
    } else {
      if (timer) { clearInterval(timer); timer = null; }
    }
  });

  // 筛选变更时立即刷新
  document.addEventListener('change', (e) => {
    if (e.target.id === 'anomaly-category-filter' || e.target.id === 'anomaly-severity-filter') {
      loadAnomalies();
    }
  });
})();
```

### 10.3 集成到 dashboard 框架

**`dashboard/base.html`** — 新增 nav tab（在`"云端"`之后，`</nav>`之前）：

```html
<button class="if-nav-item" data-tab="tab-anomaly" onclick="switchTab('tab-anomaly')">
  <svg width="22" height="22"><use href="#s-alert"/></svg>
  <span>异常</span>
</button>
```

**`dashboard/__init__.py`** — 在 fragment 加载列表中追加 `"anomaly"`：

```python
# 在 _cached_html 组装循环中
for frag_name in ("overview", "inference", "monitor", "deploy", "cloud", "anomaly"):
```

同时需要在 `base.html` 的 SVG 符号定义区添加 `#s-alert`：

```html
<svg style="display:none">
  <symbol id="s-alert" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 8v4M12 16h.01"/>
    <path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/>
  </symbol>
</svg>
```

### 10.4 涉及修改

| 文件 | 操作 |
|------|------|
| 新增 `dashboard/fragments/anomaly.html` | 新建（~35 行） |
| 新增 `dashboard/js/anomaly.js` | 新建（~60 行） |
| `dashboard/base.html` | +1 nav tab 按钮 + SVG 符号 |
| `dashboard/__init__.py` | +`"anomaly"` 到 fragment 加载列表 |

### 10.5 测试

```python
# curl http://127.0.0.1:8999/ → dashboard HTML 中包含 "tab-anomaly" 元素
# curl http://127.0.0.1:8999/api/anomalies → 返回 JSON
# 注入 1 条 anomaly → dashboard "异常" tab 显示该事件
```

### 10.6 风险评估

**风险等级：极低。** 纯 UI 变更。不修改任何 API 逻辑或请求处理路径。异常数据来自 R9 的只读 API。

---

## R1. request_log 补盲区（P0 最高优先）

### 1.1 现状分析

`_handle_messages`（handler.py:251）中的变量初始化：

```python
req_id = pm.new_request_id()          # 行 270
req_start = time.monotonic()          # 行 271
key_name = pm.auth.key_name(...)      # 行 272
self._req_id = req_id                 # 行 274
self._req_start = req_start           # 行 275
```

`RequestLog` dataclass 字段（request_logger.py:27-42）：
`req_id, key_name, model, status, ttft_ms, tokens_in, tokens_out, duration_ms, route, cloud_provider, error, timestamp, ts`

**已有 request_log 写的路径**：
- 行 293：auth 401 — ✅ 有 RequestLog。
- 行 399-408：cloud 成功 — ✅ 写 RequestLog。
- 行 446-455：cloud fallback — ⚠️ R8 削除此路径。
- 行 477-488：local 成功 — ✅ 写 RequestLog。

**缺失 request_log 的早退路径**（全部在 `_handle_messages` 和 `handle_chat` 中）：

| 文件 | 函数 | 行号 | 状态码 | 场景 |
|------|------|------|--------|------|
| handler.py | `_handle_messages` | 325-330 | 503 | SWITCHING guard |
| handler.py | `_handle_messages` | 352-353 | 409 | auto-switch 冲突 |
| handler.py | `_handle_messages` | 362-369 | 503 | auto-switch 失败 |
| handler.py | `_handle_messages` | 373-379 | 503 | 模型未激活且 AUTO_SWITCH=off |
| handler.py | `_handle_messages` | 458 | 503 | 无可用路由 |
| chat_handlers.py | `handle_chat` | 238-239 | 409 | switch already in progress |
| chat_handlers.py | `handle_chat` | 245-246 | 503 | Cannot switch |
| chat_handlers.py | `handle_chat` | 277-278 | 404 | Unknown model |

### 1.2 设计方案

在每个早退分支的 `_send_json` 调用之前，增加一行 `pm.logger.log(RequestLog(...))`。

字段取值规范：

| RequestLog 字段 | 取值 |
|----------------|------|
| `req_id` | 方法开始时已分配的 `req_id` |
| `key_name` | `key_name` 变量 |
| `model` | `original_model` 或 `model` 变量 |
| `status` | 返回的状态码（503/409/404 等） |
| `route` | 所有早退路径设 `"local"`（不涉及云端转发） |
| `error` | 结构化错误代码，如 `"model_switching"`、`"auto_switch_failed"`、`"unknown_model"` 等 |
| `duration_ms` | `(time.monotonic() - req_start) * 1000` |
| 其它字段 | 默认值（0 / None） |

**错误代码规范**（`error` 字段）：

| 场景 | error 代码 |
|------|-----------|
| SWITCHING guard | `"model_switching"` |
| auto-switch conflict (409) | `"switch_in_progress"` |
| auto-switch 失败 (503) | `"auto_switch_failed"` |
| 模型未激活且 AUTO_SWITCH=off | `"model_not_active"` |
| 无可用路由 | `"no_route"` |
| 未知模型 (404) | `"unknown_model"` |
| chat 无法切换 | `"cannot_switch"` |

### 1.3 测试

```python
# 对每个早退分支发送请求，断言 request_log 有对应 status 和 error 记录
# 每个早退分支的 error 代码符合上述规范
```

### 1.4 风险评估

**风险等级：极低。** 仅增加日志写入，不改变任何请求处理逻辑。`RequestLog` 写入是幂等的，失败不会抛出异常。

---

## R2. 云端转发退避重试（P0）

### 2.1 现状分析

`forward_to_cloud`（forwarder.py:173-273）是**单次**请求：

```python
def forward_to_cloud(handler, data, provider_cfg, cloud_model, protocol="openai", ...):
    body = json.dumps(data).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    resp = urlopen(req, timeout=provider_cfg.timeout)   # 单次，失败即返回
```

baidu-codingplan 云端间歇性返回 429（限频）、500（内部错误）或连接超时——直接透传给客户端导致"莫名其妙的 API error"。

已有的本地重试链（`forward_anthropic_local`，行 279-349）：
- 3 次尝试（`UPSTREAM_LOCAL_RETRIES=2`，即 1+2=3 次调用）
- 指数退避：`exponential_backoff(attempt)` → 0.5s, 1s, ...
- 重试条件：`should_retry_on_status`（5xx / 408 / 429）+ 连接异常

**云端转发缺失的正是同样的重试逻辑。**

### 2.2 重试策略设计

#### 2.2.1 可重试的错误

| 错误类型 | 可重试？ | 理由 |
|---------|---------|------|
| HTTP 429 Too Many Requests | ✅ | 临时限频，退避后通常恢复 |
| HTTP 5xx（500/502/503/504） | ✅ | 服务器内部错误，短暂故障后可恢复 |
| 连接超时 / 连接被拒 | ✅ | 网络抖动或服务临时不可用 |
| 其他 HTTP 4xx（400/401/403/404） | ❌ | 客户端错误，重试不会成功 |
| 流式响应中 error（mid-stream） | ❌ | 响应头已发给客户端，无法撤回 |

#### 2.2.2 重试次数与退避

沿用本地路径的配置：
- 重试次数：`iff.yaml: cloud_retry.max_retries`，默认 3（= 1 次初始 + 2 次重试）。
- 退避序列：`[0.5, 1.0, 2.0]` 秒（指数退避，与 `exponential_backoff` 一致）。

#### 2.2.3 流式响应的特殊处理

关键约束：**流式响应一旦调用 `pipe_stream_response` → 响应头已发出 → 不可撤回。**

重试逻辑在 `pipe_stream_response` 调用之前完成。HTTP 状态码在响应头中，在读取 body 之前。所以检测到 429/5xx 时 body 还没读，安全关闭连接即可重试。

```
for attempt in range(max_retries):
    try:
        resp = urlopen(req, timeout=...)
        if should_retry_on_status(resp.status) and attempt < max_retries:
            resp.close()
            sleep(backoff)
            continue
        if was_stream:
            pipe_stream_response(handler, resp, sse_buf)
        else:
            handle_json_response(...)
        return CloudResult(200, ...)
    except (TimeoutError, ConnectionError) as e:
        if attempt < max_retries:
            sleep(backoff)
            continue
        raise
```

### 2.3 配置

```yaml
# iff.yaml（新增）
cloud_retry:
  max_retries: 3         # 共 3 次尝试（1 次初始 + 2 次重试）
  backoff_base: 0.5       # 退避基数（秒），序列为 base * 2^attempt
```

### 2.4 涉及修改

- `forwarder.py`：在 `forward_to_cloud` 的 `urlopen` 调用外围加重试循环。
- `config.py`：新增 `CLOUD_RETRY_MAX_RETRIES` 默认常量。

### 2.5 测试

```python
# mock urlopen 依次返回 429→200，断言重试 1 次后成功
# mock 3 次全是 500，断言最终返回 500 且 CloudResult.error 不为 None
# mock 返回 400（不可重试），断言不重试直接返回
```

### 2.6 风险评估

**风险等级：低。** 重试逻辑仅重构 `forward_to_cloud` 的 try/except 块，对外接口不变。

---

## R3. 提高云端默认超时 60s → 600s（P0）

### 3.1 现状分析

`ProviderConfig.timeout` 的默认路径（cloud_discovery.py）：

| 位置 | 代码 | 当前默认值 |
|------|------|-----------|
| ProviderConfig dataclass | `timeout: int = 60` | 60s |
| YAML 加载（provider） | `timeout=pcfg.get("timeout", 60)` | 60s |
| YAML 加载（preset） | `timeout=pdata.get("timeout", 60)` | 60s |
| 实际使用（forward_to_cloud） | `urlopen(req, timeout=provider_cfg.timeout)` | 60s |

用户当前的 baidu-codingplan 提供商 YAML **没有配置 timeout 字段**，因此实际超时为 **60 秒**。64k token 长输出（约 2-4 分钟）会被 60s 掐断。

### 3.2 设计决策

将三处默认值从 60 改为 600：

- `ProviderConfig.timeout: int = 600`（dataclass 字段默认值）。
- `timeout=pcfg.get("timeout", 600)`（YAML 加载处）。
- `timeout=pdata.get("timeout", 600)`（preset 加载处）。

per-provider 仍可通过 YAML 的 `timeout` 字段覆盖为更短或更长的值。

### 3.3 涉及修改

`cloud_discovery.py` 的 3 处修改（第 100 行、第 258 行、第 586 行附近）。

### 3.4 测试

```python
# 构造未设 timeout 的 provider YAML → timeout == 600
# 构造 timeout: 120 的 provider YAML → timeout == 120
```

### 3.5 风险评估

**风险等级：极低。** 纯默认值修改。600s 超时会长时间占用 worker 线程——在当前的 threaded server 模型下是唯一隐忧，待 R7 async 升级后消除。

---

## R4. per-model 并发池（P1）

### 4.1 现状分析

```python
# ratelimit.py: DualGateLimiter.__init__
self._concurrency = threading.Semaphore(max_concurrent)  # 全局信号量，所有模型共享
```

所有模型共享一个并发池。当调用方向多个不同模型同时发送批量请求时，它们互相争抢这个全局池。

### 4.2 设计

把全局信号量改为 **per-model 信号量字典** + 可选的全局总上限：

```python
class DualGateLimiter:
    def __init__(self, ..., model_concurrent: int = 4, global_max: int | None = None):
        self._per_model: dict[str, threading.Semaphore] = {}
        self._model_default = model_concurrent
        self._global_sem = threading.Semaphore(global_max) if global_max else None
        self._lock = threading.Lock()

    def acquire(self, model: str, ...) -> GateResult:
        # 1. RPM 门（不变）
        # 2. 全局并发门（如果配置了 global_max）
        # 3. per-model 并发门
```

配置（`iff.yaml`）：

```yaml
rate_limit:
  max_concurrent: auto       # 每模型并发池大小
  global_max_concurrent: 0   # 全局总上限，0=不限
```

### 4.3 涉及修改

- `ratelimit.py::DualGateLimiter`：重构 `acquire`/`_release`。
- `config.py`：新增 `MODEL_CONCURRENT_DEFAULT` 常量和 `iff.yaml` 解析。

### 4.4 测试

```python
# 并发 4 个模型 A 请求（per-model limit=2），前 2 通过后 2 阻塞，模型 B 请求不受影响
```

### 4.5 风险评估

**风险等级：中。** 改动信号量逻辑，涉及 `acquire`/`release` 的对称性。

---

## R5. 精确匹配响应缓存（P1）

### 5.1 缓存键设计

键 = `sha256(f"{model}\n{canonical_json(body)}\n")`

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 包含 model | ✅ | 不同模型同一 prompt 响应不同 |
| JSON 规范化 | ✅ `sort_keys=True` | 消除 key 顺序差异 |
| 排除 stream 参数 | ✅ | stream=true/false 归一化为同一缓存 |
| 包含 max_tokens | ✅ | 输出截断长度不同不应共享 |
| 仅缓存 temperature=0 | ✅（默认） | 非确定输出的缓存不安全 |

### 5.2 实现（自建 LRU）

```python
# response_cache.py
from collections import OrderedDict
import threading

class LRUCache:
    def __init__(self, maxsize=1000):
        self._cache = OrderedDict()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        return None

    def put(self, key, value):
        with self._lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def __len__(self):
        return len(self._cache)
```

### 5.3 涉及修改

- 新增 `inferfabric/proxy/response_cache.py`。
- `config.py`：新增 `CACHE_ENABLED` / `CACHE_MAX_ENTRIES`。
- `handler.py` + `chat_handlers.py`：auth 后、SWITCHING guard 前插入缓存查找。

### 5.4 测试

```python
# 插入超过 maxsize → 最旧条目被淘汰
# 相同 body 两次请求 → 第二次命中 X-IFF-Cache: hit
# stream=true → 不缓存不命中
# temperature=0.7 → 不缓存（allow_nonzero_temp=true 除外）
```

### 5.5 风险评估

**风险等级：低。** 缓存是无副作用的读-写通路。

---

## R6. 标准 /metrics（Prometheus 文本格式）+ 可选 OTel 导出（P1）

### 6.1 方案

**零依赖自研 Prometheus 文本格式**。Prometheus 文本格式极其简单：

```
# HELP iff_requests_total Total requests served
# TYPE iff_requests_total counter
iff_requests_total{model="qwen38-27b",route="local",status="200"} 1234
```

指标清单：

| 指标名 | 类型 | Labels |
|--------|------|--------|
| `iff_requests_total` | counter | model, route, status |
| `iff_request_duration_ms` | histogram | model, route |
| `iff_tokens_in_total` | counter | model |
| `iff_tokens_out_total` | counter | model |
| `iff_ttft_ms` | histogram | model |
| `iff_errors_total` | counter | model, error |
| `iff_active_requests` | gauge | model |

**OTel 导出**：只有在 `iff.yaml: otel.enabled == true` 时才惰性 `import opentelemetry`。

### 6.2 涉及修改

- 新增 `inferfabric/proxy/metrics_exporter.py`：Prometheus 文本格式构建器。
- `handler.py`：新增 GET 路由 `/metrics`。
- `telemetry.py`：新增 `OtelExporter` 类。
- `config.py`：新增 `OTEL_ENABLED` / `OTEL_ENDPOINT` 配置。

### 6.3 风险评估

**风险等级：低。** /metrics 是只读端点，不修改任何请求路径。

---

## R7. 服务端 async 化 + 多副本 + 最空闲/延迟路由（P2）

### 7.1 现状

当前服务端：

```python
class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True
```

- 每请求一个线程（无上限），高并发下线程数爆炸。
- 所有 I/O 阻塞：`urlopen`、`HTTPConnection`——阻塞时占用整个线程。

### 7.2 框架选型

**推荐：aiohttp**。自带 HTTP server + async client，一石二鸟。

不选 uvicorn/FastAPI：IFF 是透明代理，不需要 ASGI 中间件链、不需要类型验证。

### 7.3 迁移路径（分三小步）

1. **Async server**（不改变路由逻辑）：
   - 新建 `async_server.py`，aiohttp 重写 server 启动和请求调度。
   - `ProxyHandler` 的 `do_GET`/`do_POST` 改为 async handle。
   - 本地转发用 aiohttp client 替代 `http.client.HTTPConnection`。
   - 云端转发用 aiohttp client 替代 `urllib.request.urlopen`。
   - 验证：所有已有测试通过，Claude Code 正常工作。

2. **多副本注册**：
   - `models.d/*.yaml` 支持 `replicas` 列表。
   - watchdog 对多副本做分别的健康探测。

3. **负载均衡路由**：
   - 在 `_forward_local` 中实现 `least_busy` / `latency` / `round_robin` 策略。

### 7.4 风险评估

**风险等级：高。** 换服务端框架是全项目最大的架构变更。分三小步做，每步独立验证；保留 threaded server 作为 fallback 模式。

---

## 实施阶段与依赖关系

```
Phase 0（已完成）
└── R0 ensure_service cooldown fix ── 已部署

Phase 1（P0，5 项并行，周期 ~3-5 天）
├── R9 AnomalyCollector ── 基础设施，3h
├── R8 削除 Step 6/7 fallback ── 依赖 R9，2h
├── R1 request_log 补盲区 ── 与 R9 共享采集点，4h
├── R2 云端退避重试 ── 正交，4h
└── R3 超时 60s→600s ── 1h

Phase 2（P1，4 项，周期 ~5-8 天）
├── R10 异常看板 dashboard tab ── 依赖 R9，3h
├── R4 per-model 并发池 ── 改动 ratelimit.py，6h
├── R5 响应缓存 ── 新增 ~60 行，4h
└── R6 /metrics + OTel ── 新增 ~200 行，8h

Phase 3（P2，1 项，周期 ~10-15 天）
└── R7 async 服务端升级 ── 最大改动，分 3 小步
```

## 配置总览（`iff.yaml` 新增字段）

```yaml
# ── 云端重试（R2）─────────────────────────
cloud_retry:
  max_retries: 3
  backoff_base: 0.5

# ── 并发控制（R4）─────────────────────────
rate_limit:
  max_concurrent: auto
  global_max_concurrent: 0

# ── 响应缓存（R5）─────────────────────────
cache:
  enabled: true
  max_entries: 1000
  allow_nonzero_temp: false

# ── 指标（R6）────────────────────────────
otel:
  enabled: false
  endpoint: ""
  service_name: "inferfabric"

# ── 负载均衡（R7）─────────────────────────
load_balance:
  strategy: least_busy
```

## 审核确认点

- [ ] R8 削除 Step 6/7 fallback → 改为 404 + anomaly record 确认？
- [ ] R9 AnomalyCollector 纯内存环形缓冲 500 条上限确认？
- [ ] R10 dashboard "异常" tab 置于"云端"之后确认？
- [ ] 实施顺序 Phase 1 (R8→R9→R10→R1→R2→R3) → Phase 2 (R4→R5→R6) → Phase 3 (R7) 确认？