# Clarifications — v47-cloud-presets

## Q1: 现有 cloud_provider.yaml 中的明文 key 如何处理？
**决策**: 启动时 `_load_config()` 已有 `${VAR}` 展开，但序列化时不做转换。v4.7 新增：`_serialize_providers()` 检测明文 → 自动转 ENV。现有明文 key 在下次 save_config 时被转换。这是非破坏性的——`_load_config` 读时展开，写时转引用。

## Q2: secrets.env 与 proxy 启动的关系？
**决策**: proxy 启动时主动 `source ~/.inferfabric/secrets.env`（在 `_load_config` 之前）。secrets.env 不存在时仅 warning，不阻塞启动。这与现有行为兼容——当前 `${BAIDU_CODINGPLAN_KEY}` 等 ENV 已经由 systemd/openclaw 注入。

## Q3: 预设与手动白名单（如千帆的 models:）如何交互？
**决策**: 预设的 `models:` 是初始白名单。添加 Provider 后，如果预设 `discovery: false`，只有预设中的模型可用。如果 `discovery: true`，自动发现的结果与预设白名单合并（预设优先，即发现到同名模型时不覆盖预设的属性如 price）。

## Q4: 同一厂商多个 Provider（如千帆不同 key）？
**决策**: 支持同一预设添加多次，但 name 必须唯一。用户选预设后可修改 name（如 `baidu-qianfan-team2`）。ENV 变量名随 name 变化：`IFF_BAIDU_QIANFAN_TEAM2_KEY`。

## Q5: 自定义中转站的 ENV 命名？
**决策**: 自定义预设无 `env_var` 字段，系统根据 Provider name 自动生成：`IFF_<NAME_UPPER>_KEY`。name 中的 `-` 转 `_`。

## Q6: 预设库是否可用户扩展？
**决策**: 是。`cloud_presets.yaml` 与 `cloud_provider.yaml` 同目录（`~/.inferfabric/`）。用户可自行添加预设。IFF 更新时不覆盖用户自定义预设（合并策略：用户文件优先）。

## Q7: GET /admin/cloud/providers 返回 key 信息的粒度？
**决策**: 返回 `key_env_var` (string) + `key_env_set` (bool)。不返回 key 值。`key_env_set` 通过 `os.environ.get(var)` 检查——如果 ENV 变量存在且非空则 True。

## Q8: Anthropic 没有 /models 端点，预设如何处理？
**决策**: 预设 `discovery: false`，需手动在预设中列 `models:`。初期 Anthropic 预设包含 Claude Sonnet 4.6 / Haiku 4.5 / Opus 4.8 三个模型的基本能力属性。

## 边界确认

- ✅ 在范围内：预设模板、ENV 转换、secrets.env、Key 状态、Dashboard UI
- ❌ 不在范围内：用量仪表盘、MCP 管理、系统托盘、50+ 中转站预设
- ⚠️ 灰区：预设库的更新机制——暂不做自动更新，随 IFF 版本发布
