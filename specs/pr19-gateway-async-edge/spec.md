# PR-19: Gateway 升级改造 — 生产级 aiohttp 异步入口（--async）

对标行业主流 LLM gateway（LiteLLM = FastAPI+uvicorn 原生 chunked+Content-Length、
可配上限；vLLM server 框架原生；Triton C++ 手动支持 chunked）。当前 stdlib
`http.server` 入口存在 5 个缺陷；本 PR 修 4 个，第 5 个（转发核心同步阻塞）
留待 PR-20。

## 问题

1. **请求体不支持 chunked**：`forwarder.read_body` 只认 Content-Length；
   chunked 分支 `rfile.read()` 无界（慢客户端可挂住线程）
2. **inbound 无 socket 超时**：slowloris 可永久钉死 worker 线程
3. **每连接一线程**：N 路并发长 SSE 流 = N 个线程直到生成结束
4. **手工 chunked 响应分帧**（hex 尺寸+CRLF 手拼、每响应 `Connection: close`）；
   ollama 原生流路径无 body 终止符（keep-alive 客户端潜在挂死）

## 变更范围

- `inferfabric/proxy/async_server.py`（整体重写，PR 主体）：
  - 混合执行模型：现有同步 route fn / forwarder 全部在 `ThreadPoolExecutor(32)`
    执行（`fn(h, pm)` 契约不变，**转发核心不动**）
  - 7 条转发路由 → `StreamResponse` 原生流式：worker 写的手工 chunked 帧经
    `_WfileSink` 解帧 → `asyncio.Queue` → 原生分帧回传（真增量 SSE，无整包缓冲）
  - 其余 ~30 条路由整包缓冲 + 完整头传播（strip 集
    `content-length`/`transfer-encoding`/`connection`，aiohttp 自管分帧/连接）
  - `client_max_size=100MB`（与 read_body 上限一致；超限 413 JSON 体与旧版逐字节一致）
  - 客户端断连：RST → `handler_cancellation` 取消 pump → sink 标记 gone →
    worker 下次 write 抛 BrokenPipeError（forwarder 既有 except 捕获）；
    普通 FIN（keep-alive 语义）不触发 —— worker 跑完上游流自然释放（有界）
  - EADDRINUSE 5×/2s 重试（复刻 `_create_server`，`_start_site_once` 留 patch 缝）
  - 生命周期对齐线程版 `main()`：excepthooks、pycache 清除、reconcile、
    health_loop、TokenStatsCollector、sd_notify READY/WATCHDOG/STOPPING
  - 修复实验版（R7 Step 1）6 个缺陷：流式整包缓冲、丢响应头、client_max_size
    默认 1MB、`path` 丢查询串、`_MockServer` 缺 watchdog、生命周期缺失
- `inferfabric/proxy/handler.py`：删 3 条死路由 `/static/*`
  （`_serve_static` 从未定义；线程模式从"静默断连"变 404 JSON；dashboard 无引用，
  资产由 `get_html()` 内联）
- `tests/unit/proxy/test_async_server.py`：新增 14 个（流式增量到达回归、
  头传播、413/404、admin guard、chunked 请求体、sink 机制、RST 断连停推）
- `tests/unit/proxy/test_handler.py`：移除 `_serve_static` 死 mock 行
- `inferfabric/__init__.py`：5.7.1 → 5.8.0
- `_deps` cp313 wheel 重建（运行时 Python 3.13，vendor 的是 cp312 wheel，
  C 扩展静默失效走纯 Python fallback）+ `scripts/rebuild-deps.sh` 可复现脚本

## 不改什么（PR-14 排除条款继续有效）

- `forwarder.py` / `chat_handlers.py` 转发循环 / embeddings/rerank 循环：**一字不动**
- 线程版入口仍是**默认**（`python -m inferfabric.proxy.handler`）；
  新引擎仅 `--async` opt-in → 回退 = 不传 flag，零成本
- watchdog / 云发现 / 限流 / 缓存 / 异常 / 遥测子系统不变
- 全部既有测试（731 个，2026-09-16 实测基线）零改动通过

## 行为差异（文档化，仅 `--async` 模式）

| 项 | 线程版 | async 边 |
|---|---|---|
| 方法不符（POST /health） | 404 not found | 404 not found（catch-all 一致；非 405） |
| 未知 worker 异常 | 静默无响应 | 干净 500 JSON |
| `Connection: close`（JSON 响应） | 每响应都有 | 消失（keep-alive 保留，aiohttp 自管） |
| 转发路由 JSON 响应 | Content-Length | 原生 TE: chunked（客户端透明） |
| `/static/*` | 静默断连（AttributeError） | 404 JSON（两模式一致，路由表已删） |
| ollama 原生流终止符 | 无（潜在挂死） | StreamResponse 原生终止 |
| 上游查询串转发 | 完整（self.path 含 query） | 完整（`path_qs` 修复实验版丢 query bug） |

## 依赖基线修订（显式声明，非静默）

- v53 "stdlib 三件套" 基线更新：aiohttp 3.14.3 + 全套传递依赖（multidict、
  yarl、attrs、frozenlist、aiosignal、aiohappyeyeballs、propcache、idna、
  cachetools）**已 vendor** 于 `_deps/`（`proxy_manager.py` import 时注入
  sys.path），本 PR 零新增 pip 依赖
- cp313 wheel 重建为**性能恢复**：C 扩展（aiohttp._http_parser 等）生效；
  回滚安全 = 删新 `.so` 自动回退纯 Python（逐包已验证 fallback），纯性能损失

## 风险 + 缓解

| 风险 | 缓解 |
|---|---|
| 转发核心回归 = 全链路挂 | 转发核心零改动 + 线程版仍默认 + 回退零成本；731 全量测试 + 14 新增；单 PR 可整体 revert |
| 事件循环被同步子系统阻塞 | 事件循环只做派发/泵送；全部阻塞 I/O 在 executor 线程；慢速客户端占协议对象不占线程 |
| 断连停推依赖 RST | RST → 取消 + 停推（已测）；普通 FIN 不触发 → worker 有界跑完上游流（300s 超时兜底，文档化） |
| 队列背压 | 1024 chunk（~8MB/流）满 → 标记断连截断（文档化）；LLM 流速率远低于此 |
| cp313 wheel 失败 | 纯 Python fallback 兜底（已验证），删 `.so` 回滚 |
| EADDRINUSE / 残留 proxy | 5×/2s 重试 + 双实例冒烟（已测） |

## 验证记录（2026-09-16）

- `tests/unit + tests/integration`：**745 passed**（基线 731 + 新增 14）
- 启动冒烟（隔离 IFF_DATA_DIR + 端口 18999，生产 :8999 不受影响）：
  `GET /` 200（169KB dashboard）、`GET /status` 200（正确识别生产 NInfer）、
  `GET /health` 200、`GET /api/snapshot` 200+ETag → If-None-Match 304、
  死路由 404、OPTIONS 204
- 流式 e2e（活 NInfer 8007）：SSE 增量到达、`data: [DONE]` 正常终止、
  非流式 200、**chunked 请求体（无 Content-Length）200 + 6 SSE chunk**
- SIGTERM 优雅关停：有序日志 + exit 0 + 端口释放
