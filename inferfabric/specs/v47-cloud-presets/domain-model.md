# Domain Model — v47-cloud-presets

## 核心领域对象

### CloudPreset (新增)

```
CloudPreset
├── id: str                    # "baidu-qianfan"
├── display_name: str          # "百度千帆"
├── icon: str                  # "🟦"
├── openai_base: str           # "https://..."
├── anthropic_base: str        # "https://..."
├── env_var: str               # "IFF_BAIDU_QIANFAN_KEY"
├── discovery: bool            # false → 手动白名单
├── models: dict[str, ModelSpec]  # 预置模型能力
└── timeout: int               # 默认 60
```

### ProviderConfig (修改)

新增字段：
```
ProviderConfig
├── ... (现有字段)
├── key_env_var: str           # 新增：API Key 对应的 ENV 变量名
└── preset_id: str | None      # 新增：来源预设 ID（可选，用于 UI 展示图标）
```

### SecretsManager (新增)

```
SecretsManager
├── secrets_path: Path         # ~/.inferfabric/secrets.env
├── load() → dict[str, str]    # 读取所有 key=value
├── write(key, value)          # 追加/更新单条
├── env_set(var_name) → bool   # 检查 ENV 是否设置
└── ensure_file()              # 创建文件 + chmod 600
```

## 数据流

```
用户选预设
  │
  ├─ GET /admin/cloud/presets → 返回预设列表
  │
  ├─ 用户填 API Key → POST /admin/cloud/providers
  │   body: { preset: "baidu-qianfan", api_key: "sk-xxx" }
  │   或: { name: "custom", api_key: "sk-xxx", openai_base: "..." }
  │
  ├─ handler.py:
  │   1. 从预设合并 base_url + discovery 配置
  │   2. 明文 key → SecretsManager.write(env_var, key)
  │   3. ProviderConfig(api_key="${ENV_VAR}", key_env_var="ENV_VAR", preset_id="...")
  │   4. 注册模型（预设 models: → _register_spec_only_models）
  │   5. save_config() → YAML 中 api_key 为 ${ENV_VAR}
  │
  └─ Dashboard 刷新 → Provider 列表展示 ENV + 状态
```

## 与现有模块的关系

```
cloud_discovery.py
  ├── CloudDiscovery (修改)
  │   ├── load_presets() → dict[str, CloudPreset]    # 新增
  │   ├── _serialize_providers() → ENV 转换           # 修改
  │   └── _env_key_for_provider(name) → str           # 新增
  │
  ├── SecretsManager (新增类)
  │   └── 独立，无 CloudDiscovery 依赖
  │
  └── cloud_presets.yaml (新文件)

proxy/handler.py (修改)
  ├── GET /admin/cloud/presets                         # 新增端点
  ├── POST /admin/cloud/providers → 支持 preset 字段   # 修改
  └── GET /admin/cloud/providers → key_env_var + key_env_set  # 修改

dashboard/ (修改)
  ├── js/app.js → cloudLoadPresets() + 预设卡片渲染
  └── fragments/cloud.html → 双模式表单
```

## 关键设计决策

1. **预设不可变原则**: 预设只用于初始化 Provider，添加后的配置完全由 cloud_provider.yaml 控制。修改预设不影响已添加的 Provider。
2. **ENV 转换时机**: 在 `save_config()`（`_serialize_providers`）时统一转换，不在 `_load_config` 时。
3. **secrets.env 追加模式**: 新 key 追加到文件末尾，同 key 则替换该行。
4. **向后兼容**: 现有 `${VAR}` 格式的 key 不受影响，只转换非 `${...}` 格式的明文。
