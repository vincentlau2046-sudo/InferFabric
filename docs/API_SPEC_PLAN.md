# OpenAPI 规范文档实施计划

> 审计结论归档 — InferFabric v5.4.0 全面审计
> 日期：2026-08-02 | 来源：项目全面审计报告

---

## 一、审计发现的问题

### 问题描述

InferFabric 项目的文档质量整体良好（README 专业完整，有大量设计评审报告、架构分析、Steering DDD 文档），但 **缺少 API 规范文档**。

| 维度 | 现状 |
|------|------|
| OpenAPI / Swagger 规范 | 完全缺失（0 个文件） |
| README 中的 API 表格 | 仅有路径+HTTP方法的粗略清单，无 request/response schema |
| 代码注释中的 API 文档 | 散布在 handler.py 各 _handle_* 方法的 docstring 中 |
| 自动化 API 文档生成 | 无 |
| 交互式文档页面 (Swagger UI / Redoc) | 无 |

### 影响

- 第三方客户端集成时只能读源码或抓包
- 无法使用 OpenAPI Generator 自动生成 SDK
- 缺少机器可读的接口契约

---

## 二、完整 API 清单

> 以下 37 个端点全部来自 inferfabric/proxy/handler.py 的 route tables 和 _handle_* 方法实现。

### 2.1 核心推理 API（OpenAI/Anthropic 兼容）

| # | 方法 | 路径 | 协议 | 说明 | 流式 |
|---|------|------|------|------|------|
| 1 | POST | /v1/chat/completions | OpenAI | 聊天补全 | 是 |
| 2 | POST | /v1/completions | OpenAI | 文本补全 -> 委派 chat | 是 |
| 3 | POST | /v1/messages | Anthropic | Messages API（含完整回退链） | 是 |
| 4 | POST | /v1/embeddings | OpenAI | 文本嵌入（auto-start 模型） | 否 |
| 5 | POST | /v1/rerank | - | 重排序 | 否 |
| 6 | GET | /v1/models | OpenAI | 列出模型（本地+云端合并） | 否 |

> 说明：/v1/messages 路由逻辑最复杂，含 6 步回退链：
> 1. SWITCHING 守卫 -> 503
> 2. 按 model 名路由到本地活跃服务
> 3. 已知模型但未活跃 -> auto-switch
> 4. Cloud Discovery -> 云端路由
> 5. 回退到第一个活跃 LLM
> 6. 最后的 Baidu 回退

### 2.2 原生后端透传 API

| # | 方法 | 路径 | 后端 | 说明 |
|---|------|------|------|------|
| 7 | POST | /api/chat | Ollama | Ollama 原生 chat（OpenAI SSE 转换） |
| 8 | POST | /api/generate | Ollama | Ollama 原生 generate |

### 2.3 控制平面 API（需 Admin Token）

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 9 | POST | /switch | 切换模型（请求体 {"model":"gemma4-31b-vl"}） |
| 10 | POST | /stop | 停止服务 |
| 11 | POST | /sleep | vLLM L2 休眠 |
| 12 | POST | /wake | vLLM 唤醒 |
| 13 | POST | /reset | 强制重置（核选项） |
| 14 | POST | /reconcile | 修复 state.db 与实际进程不一致 |
| 15 | POST | /deploy | 自动部署模型 |
| 16 | POST | /pull | 拉取模型（支持 ollama 格式） |
| 17 | POST | /reload-config | 热加载 models.d/*.yaml |

### 2.4 Cloud Provider 管理 API（需 Admin Token）

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 18 | GET | /admin/cloud/providers | 列出所有提供商及状态 |
| 19 | POST | /admin/cloud/providers | 添加提供商（支持预设/手动模式） |
| 20 | DELETE | /admin/cloud/providers | 删除提供商 |
| 21 | GET | /admin/cloud/presets | 列出 9 个预设厂商 |
| 22 | POST | /admin/cloud/reload | 热加载 cloud_provider.yaml |
| 23 | POST | /admin/cloud/discover | 手动触发云端模型发现 |
| 24 | POST | /admin/cloud/test | 测试 provider 连接（含 SSRF 保护） |

### 2.5 监控 & 仪表盘 API

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 25 | GET | /health | 简单健康检查 {"status":"ok","gpu_mode":"idle"} |
| 26 | GET | /status | 完整系统状态（GPU、服务、PID） |
| 27 | GET | /models | 本地配置的模型列表 |
| 28 | GET | /system | 系统信息 |
| 29 | GET | /api/metrics | 聚合指标（支持 ?window=1h|24h|7d|all） |
| 30 | GET | /api/request_log | 请求日志查询 |
| 31 | GET | /vllm_metrics | vLLM Prometheus 指标（?port=8005） |
| 32 | GET | /watchdog_status | Watchdog 状态 |
| 33 | GET | /history | 模型切换历史（最近 30 条） |

### 2.6 仪表盘静态资源

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 34 | GET | / | Dashboard HTML |
| 35 | GET | /static/style.css | CSS 样式 |
| 36 | GET | /static/app.js | 主 JS |
| 37 | GET | /static/monitor.js | 监控 JS |

---

## 三、方案设计与对比

### 方案 A：纯 OpenAPI YAML 文件（最小侵入）

文件结构：

```
inferfabric/
└── api-spec/
    ├── openapi.yaml             # 主规范（约 500 行）
    ├── components/
    │   └── schemas.yaml         # 共享 Schema（约 150 行）
    └── README.md                # 使用说明
```

**优点：**
- 零代码侵入，只需新增 YAML 文件
- 可用 redoc-cli 或 swagger-ui 生成交互式文档
- YAML 与 Python 代码解耦，可独立维护

**缺点：**
- 运行时无法直接访问规范
- 需要手工维护与代码同步

### 方案 B：运行时注入 + 交互式 UI（推荐）

在方案 A 基础上增加：

```
inferfabric/
├── api-spec/
│   ├── openapi.yaml
│   └── components/schemas.yaml
├── inferfabric/
│   └── api_spec.py              # 加载 YAML + 提供给 handler
```

在 handler.py 增加两条路由：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/openapi.json | 返回 OpenAPI JSON（运行时可用） |
| GET | /api/docs | Redoc 交互式文档页面 |

**优点：**
- curl/clients 可直接获取规范
- 仪表盘左侧栏可增加 "API Docs" 导航项
- 一次实现，两个端点

**缺点：**
- 需要增加 api_spec.py 和 2 行路由
- 需要将 Redoc HTML 内联或作为静态文件

### 方案 C：完整工程化方案（高阶）

在方案 B 基础上增加：
- Makefile 目标：api-validate（lint）、api-preview（预览）
- CI 检查：PR 时验证 OpenAPI lint
- README 中增加 API 文档章节和徽章

---

## 四、推荐方案 B 的实施步骤

### 第 1 步：创建目录结构

```
inferfabric/
├── api-spec/
│   ├── openapi.yaml
│   └── components/
│       └── schemas.yaml
├── inferfabric/
│   ├── api_spec.py              # [新文件]
│   └── proxy/handler.py         # [+ 2 行路由]
```

### 第 2 步：编写 api_spec.py

```python
"""API 规范加载器 — 加载 OpenAPI YAML 并提供给 handler。"""
from pathlib import Path
import yaml
import json

_SPEC_PATH = Path(__file__).parent.parent / "api-spec" / "openapi.yaml"
_spec: dict | None = None


def get_openapi_spec() -> dict:
    """获取 OpenAPI 规范字典（带版本注入）。"""
    global _spec
    if _spec is None:
        with open(_SPEC_PATH) as f:
            raw = yaml.safe_load(f)
        # 注入动态版本号
        from . import __version__
        raw["info"]["version"] = __version__
        _spec = raw
    return _spec
```

### 第 �步：在 handler.py 增加路由

在 _GET_ROUTES 字典中增加：

```python
"/api/openapi.json": lambda h, pm: h._send_json(get_openapi_spec()),
```

可选增加 Redoc 页面：

```python
"/api/docs": lambda h, pm: h._send_html(REDOC_HTML_PAGE),
```

### 第 4 步：编写 openapi.yaml（核心工作量）

```yaml
openapi: 3.1.0
info:
  title: InferFabric - LLM InferenceGateway
  description: | 
    单GPU LLM推理网关。模型即插件，三态GPU状态机，
    macOS Dashboard，9云端预设。
  version: "5.4.0"   # 运行时会自动注入
  license:
    name: MIT

servers:
  - url: http://localhost:8999
    description: Local IFF proxy

security:
  - {}                       # 不需要认证的端点
  - admin_token: []          # 控制平面端点

paths:
  /v1/chat/completions:
    post:
      summary: OpenAI 兼容聊天补全
      operationId: createChatCompletion
      tags: [Inference]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ChatRequest'
      responses:
        '200':
          description: 非流式响应
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ChatResponse'
        '200-stream':
          description: 流式 SSE 响应
          content:
            text/event-stream:
              schema:
                $ref: '#/components/schemas/ChatChunk'

  /switch:
    post:
      summary: 切换模型
      operationId: switchModel
      tags: [Control]
      security:
        - admin_token: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                model:
                  type: string
                  description: 目标模型名或 "idle"
                  example: gemma4-31b-vl
              required: [model]
      responses:
        '200':
          description: 切换结果
          content:
            application/json:
              schema:
                type: object
                properties:
                  status:
                    type: string
                    enum: [switched, already_active, error]
                  model:
                    type: string
                  message:
                    type: string

components:
  securitySchemes:
    admin_token:
      type: apiKey
      in: header
      name: X-Admin-Token
      description: 控制平面 API 的 Admin Token

  schemas:
    ChatRequest:
      type: object
      properties:
        model: { type: string, example: gemma4-31b-vl }
        messages:
          type: array
          items: { $ref: '#/components/schemas/Message' }
        stream: { type: boolean, default: false }
        max_tokens: { type: integer, default: 4096 }
        temperature: { type: number, default: 0.7 }
        top_p: { type: number, default: 1.0 }
      required: [model, messages]

    Message:
      type: object
      properties:
        role:
          type: string
          enum: [system, user, assistant, tool]
        content: { type: string }
      required: [role, content]

    ChatResponse:
      type: object
      properties:
        id: { type: string }
        object: { type: string, enum: [chat.completion] }
        created: { type: integer }
        model: { type: string }
        choices:
          type: array
          items:
            type: object
            properties:
              index: { type: integer }
              message:
                type: object
                properties:
                  role: { type: string }
                  content: { type: string, nullable: true }
              finish_reason: { type: string, nullable: true }
        usage:
          type: object
          properties:
            prompt_tokens: { type: integer }
            completion_tokens: { type: integer }
            total_tokens: { type: integer }

    ErrorResponse:
      type: object
      properties:
        error: { type: string }
        status: { type: string }
      required: [error]
```

### 第 5 步：更新 README

在 ## API Endpoints 表格下方增加：

```markdown
### OpenAPI 规范

完整 API 规范（OpenAPI 3.1）：

- JSON: GET /api/openapi.json
- 交互式文档: GET /api/docs（Redoc UI）
- YAML 源文件: api-spec/openapi.yaml
```

---

## 五、预估工作量

| 文件 | 预估行数 | 内容 | 难度 |
|------|----------|------|------|
| api-spec/openapi.yaml | 约 600 行 | 所有 path + method + parameter + schema | 体力活 |
| api-spec/components/schemas.yaml | 约 200 行 | 共享 Schema 定义 | 中等 |
| inferfabric/api_spec.py | 约 30 行 | YAML 加载器 | 简单 |
| inferfabric/proxy/handler.py | +2 行 | 加两条 GET 路由 | 简单 |
| README.md | +5 行 | 更新文档 | 简单 |
| 总计 | 约 850 行新代码（其中 90% YAML） | | |

---

## 六、相关参考

- OpenAPI 3.1 规范: https://spec.openapis.org/oas/v3.1.0
- Redoc: https://redocly.com/redoc
- Swagger UI: https://swagger.io/tools/swagger-ui/
- InferFabric README API 表格: README.md（现有）
- 完整路由实现: inferfabric/proxy/handler.py（1388 行）
