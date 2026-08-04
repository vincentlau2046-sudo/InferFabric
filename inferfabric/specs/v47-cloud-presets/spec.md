# Feature Spec: v47-cloud-presets

## 概述

为 IFF 云厂商管理引入预设模板库，用户从预设选择厂商→填 API Key→一键添加，替代当前纯手动填表模式。同时强制 API Key 通过环境变量引用存储，杜绝明文 key 入 YAML。

## 问题空间

### 当前痛点

1. **添加门槛高**：用户需自行查找厂商 base URL、endpoint 格式，手动填写 4 个字段
2. **API Key 明文存储**：`cloud_provider.yaml` 中 `api_key` 直接存明文，序列化时无 ENV 转换
3. **无模型能力预置**：即使手动添加，仍需依赖 `/models` 发现（部分厂商如千帆的发现结果不可信）
4. **Dashboard 无 Key 状态反馈**：不知道 ENV 是否设置，key 是否有效

### 目标状态

- 用户选预设→只填 API Key→一键添加（2 步替代 4 字段手工）
- YAML 中只存 `${ENV_VAR}` 引用，明文 key 自动转为 ENV + 写入 `secrets.env`
- 预设内置已知模型能力，跳过不可信的自动发现
- Dashboard 展示 ENV 变量名 + 状态（✅ 已设置 / ⚠️ 未设置）

## 预设厂商列表

| 序号 | 名称 | ID | 图标 | ENV 变量 | 发现模式 |
|------|------|----|------|---------|---------|
| 1 | 百度千帆 | baidu-qianfan | 🟦 | IFF_BAIDU_QIANFAN_KEY | 手动白名单 |
| 2 | 火山方舟 | volcengine | 🌋 | IFF_VOLCENGINE_KEY | 自动发现 |
| 3 | 阿里百炼 | ali-bailian | 🟦 | IFF_ALI_BAILIAN_KEY | 自动发现 |
| 4 | DeepSeek | deepseek | 🐋 | IFF_DEEPSEEK_KEY | 自动发现 |
| 5 | 智谱AI | zhipu | 🐋 | IFF_ZHIPU_KEY | 自动发现 |
| 6 | Moonshot | moonshot | 🌙 | IFF_MOONSHOT_KEY | 自动发现 |
| 7 | OpenAI | openai | 🟢 | IFF_OPENAI_KEY | 自动发现 |
| 8 | Anthropic | anthropic | 🟠 | IFF_ANTHROPIC_KEY | 手动（无 /models） |
| 9 | 自定义/中转站 | custom | 🔗 | 用户自定义 | 自动发现 |

## 功能需求

### FR-1: 预设加载与展示

- 新增 `cloud_presets.yaml`，随 IFF 发布，存放预设模板
- `CloudDiscovery` 新增 `load_presets()` 方法
- `GET /admin/cloud/presets` API 返回预设列表
- Dashboard 添加 Provider 区域渲染预设卡片网格

### FR-2: 预设式添加 Provider

- 用户选择预设 → UI 预填 base URL + 显示推荐 ENV 变量名
- 用户只需输入 API Key
- POST `/admin/cloud/providers` 新增 `preset` 字段，后端从预设合并配置
- 预设含 `models:` 时，添加后直接注册模型（跳过发现）
- 预设 `discovery: true` 时，添加后自动触发发现

### FR-3: API Key ENV 强制转换

- `_serialize_providers()` 检测明文 key → 自动转为 `${IFF_<NAME>_KEY}` 格式
- 明文 key 写入 `~/.inferfabric/secrets.env`（chmod 600）
- `secrets.env` 不存在时自动创建，proxy 启动时 source
- 预设指定 `env_var` 字段作为推荐变量名

### FR-4: Key 状态反馈

- `GET /admin/cloud/providers` 返回每个 provider 的 `key_env_var` + `key_env_set` (bool)
- Dashboard Provider 列表展示 ENV 变量名 + ✅/⚠️ 状态
- 不返回 key 值（即使已解密也不展示）

### FR-5: 手动模式保留

- 保留原有手动填写表单，作为"方式二"
- 手动模式下同样强制 ENV 转换

## 验收标准

### AC-1: 预设添加
```gherkin
Given 用户打开 Dashboard 云管理页面
When 用户选择"百度千帆"预设并输入 API Key "sk-xxx"
Then Provider 被添加，YAML 中 api_key 为 "${IFF_BAIDU_QIANFAN_KEY}"
And secrets.env 中写入 IFF_BAIDU_QIANFAN_KEY=sk-xxx
And 预设中的 4 个模型被直接注册
```

### AC-2: 明文 key 自动转换
```gherkin
Given 用户通过手动模式添加 Provider "my-relay"
And api_key 输入为明文 "sk-abc123"
When 提交添加
Then YAML 中 api_key 为 "${IFF_MY_RELAY_KEY}"
And secrets.env 中追加 IFF_MY_RELAY_KEY=sk-abc123
```

### AC-3: Key 状态展示
```gherkin
Given Provider "baidu-qianfan" 已配置
And secrets.env 中 IFF_BAIDU_QIANFAN_KEY 已设置
When 用户查看 Dashboard Provider 列表
Then 显示 "Key: ${IFF_BAIDU_QIANFAN_KEY} ✅ 已设置"
```

### AC-4: ENV 未设置告警
```gherkin
Given Provider "volcengine" 已配置
And secrets.env 中 IFF_VOLCENGINE_KEY 未设置
When 用户查看 Dashboard Provider 列表
Then 显示 "Key: ${IFF_VOLCENGINE_KEY} ⚠️ ENV 未设置"
```

### AC-5: 向后兼容
```gherkin
Given 现有 cloud_provider.yaml 中 api_key 为 "${BAIDU_CODINGPLAN_KEY}"
When proxy 加载配置
Then 正常展开环境变量，行为不变
```

## 非目标

- 不做桌面 App / 系统托盘（CC-Switch 的领域）
- 不做 MCP/Skills 管理（不属于 IFF）
- 不做 50+ 中转站预设（仅内置主流官方平台）
- 不做用量仪表盘（已有 request_log_db + metrics）

## 影响范围

| 文件 | 改动类型 |
|------|---------|
| `cloud_presets.yaml` (新) | 预设库 |
| `cloud_discovery.py` | 加载预设 + ENV 转换 + secrets.env |
| `proxy/handler.py` | 新 API 端点 + Key 状态 |
| `dashboard/js/app.js` | 预设卡片 + Key 状态 UI |
| `dashboard/fragments/cloud.html` | 双模式表单 |
