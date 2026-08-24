# InferFabric（IFF）推广指南 — 每一步怎么做

> 配套文档：docs/DSH_PLUGIN_PLAN.md（插件化方案）。本指南只讲推广侧。

## 0. 现有资产（已就位，可直接用）

- GitHub 仓库：github.com/vincentlau2046-sudo/InferFabric（MIT 许可）
- 当前版本：v5.5.1（README 已含 SVG 架构图 + Dashboard 截图）
- OpenAPI 3.1 规范（37 端点 / 46 schemas），运行期由 GET /api/openapi.json 提供
- 第三方依赖仅 PyYAML（其余全为 Python 标准库）— PyPI 打包只需声明 PyYAML
- models.d/ 下有 13 个模型定义（qwen38-27b-abliterated 为当前活跃模型）

## 1. PyPI 打包与发布

**Step 1 — 创建 `pyproject.toml`（IFF 仓库根目录）：

    [build-system]
    requires = ["setuptools>=61", "wheel"]
    build-backend = "setuptools.build_meta"

    [project]
    name = "inferfabric"
    version = "5.5.1"
    description = "Single-GPU LLM inference gateway (vLLM/SGLang/Ollama/ComfyUI/TTS/ASR)"
    readme = "README.md"
    requires-python = ">=3.9"
    license = { text = "MIT" }
    dependencies = ["PyYAML>=6.0"]
    authors = [{ name = "Vincent Lau" }]

    [project.scripts]
    inferfabric = "inferfabric.cli:main"

    [tool.setuptools]
    packages = ["inferfabric"]

**Step 2 — 本地验证安装**：`pip install .`，确认 `inferfabric` 命令可用。

**Step 3 — 构建 + 上传**：
    pip install build twine
    python -m build            # 在 dist/ 生成 sdist + wheel
    twine upload dist/*        # 需 PyPI 账号（可先传 test.pypi.org 验证）

## 2. Show HN 帖子

- 标题：`Show HN: InferFabric – a single-GPU LLM inference gateway`
- 正文模板（可直接粘贴）：

    I built a single-GPU LLM inference gateway (InferFabric). It fronts vLLM / SGLang / Ollama / ComfyUI / TTS / ASR on one GPU, with a 3-state GPU state machine, OpenAI + Anthropic dual-protocol routing, cloud-provider presets, and a two-level (DualGate) rate limiter. MIT, Python, zero heavy deps (only PyYAML).

    - GitHub: github.com/vincentlau2046-sudo/InferFabric
    - PyPI: inferfabric (pip install inferfabric)
    - OpenAPI 3.1 spec served at GET /api/openapi.json

- 发帖时机：美东上午 8 点前（HN 流量峰值）。

## 3. Reddit r/LocalLLaMA

- 帖子标题：`I built a single-GPU LLM gateway (InferFabric)`
- 内容：架构图（README 的 SVG）+ Dashboard 截图 + 安装方式（PyPI / GitHub）+ 特性列表
- 置顶评论放链接（GitHub + PyPI）。

## 4. X（Twitter）串帖

- 首条（钩子）："One GPU. Every engine. vLLM, SGLang, Ollama, ComfyUI, TTS, ASR — behind one gateway."
- 后续 4 条：GPU 三态状态机 / 双协议路由 / 云 provider 预设 / DualGate 限流
- 末条：链接（GitHub + PyPI + OpenAPI）
- 标签：#LocalLLM #vLLM #LLMGateway

## 5. DSH 网站集成页（DSH checkout 内）

- DSH 的 `website/` 是 VitePress 站点（dev 端口 5173）。
- 新增 `website/integrations/inferfabric.md`（或放在 docs 目录并注册进 `.vitepress` sidebar）。
- 页面内容："Running DeepSeek Harness agents on InferFabric" — 把 DSH 的 LLM provider base_url 指向 `http://127.0.0.1:8999/v1`，model 用 `qwen38-27b-abliterated`。
- 构建验证：`pnpm run docs:build`（vitepress build），在 4173 端口 preview 检查页面。

## 6. GitHub Release Notes

- 在 GitHub 仓库为 v5.5.1 创建 Release，notes 写增量特性 + 架构图 + Dashboard 截图。
- 可选：把 OpenAPI 规范（api-spec/）作为 release 附件。

## 7. 旗舰演示（博客 / 视频）

- 主题："DeepSeek Harness agents running on InferFabric"
- 录屏内容：DSH agent 的 LLM 调用经 IFF :8999 路由到 vLLM（qwen38-27b-abliterated）
- 发布：博客 + Bilibili / YouTube

## 8. 建议执行顺序

1. PyPI 打包（前置，Step 1–3）
2. GitHub Release Notes（v5.5.1）
3. DSH 网站集成页
4. Show HN + Reddit + X 串帖（同一发布窗口）
5. 旗舰演示视频（最后压轴）
