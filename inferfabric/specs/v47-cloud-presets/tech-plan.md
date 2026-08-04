# Tech Plan — v47-cloud-presets

## 实现策略

分 5 个任务，按依赖顺序执行。每个任务完成后可独立验证。

## 任务分解

### T1: cloud_presets.yaml + SecretsManager

**文件**: `cloud_presets.yaml` (新), `cloud_discovery.py` (修改)

**改动**:
1. 创建 `cloud_presets.yaml`，9 个预设
2. 在 `cloud_discovery.py` 中新增 `CloudPreset` dataclass + `load_presets()` + `SecretsManager` 类
3. `_serialize_providers()` 中添加明文 key → ENV 转换逻辑
4. proxy 启动时 source secrets.env

**验证**: `python3 -c "from inferfabric.cloud_discovery import load_presets, SecretsManager; ..."`

### T2: handler.py API 端点

**文件**: `proxy/handler.py` (修改)

**改动**:
1. `GET /admin/cloud/presets` — 返回预设列表
2. `POST /admin/cloud/providers` — 支持 `preset` 字段，从预设合并配置
3. `GET /admin/cloud/providers` — 返回 `key_env_var` + `key_env_set`

**验证**: `curl` 测试 3 个端点

### T3: Dashboard UI — 预设选择

**文件**: `dashboard/fragments/cloud.html` (修改), `dashboard/js/app.js` (修改)

**改动**:
1. cloud.html: 添加预设卡片网格区域
2. app.js: `cloudLoadPresets()` + 预设选择交互 + Key ENV 状态展示

**验证**: 浏览器查看 Dashboard

### T4: 端到端集成测试

**文件**: `tests/test_cloud_presets.py` (新)

**改动**: 预设添加流程 + ENV 转换 + secrets.env 读写 + Key 状态查询

**验证**: `pytest tests/test_cloud_presets.py`

### T5: Review + 生产合入

**流程**: OpenCode review → 问题修复 → review 通过 → diff → cp 合入生产

## 技术细节

### secrets.env 格式

```bash
# IFF Cloud Provider API Keys — 自动生成，勿手动编辑
# 文件权限: 600 (仅所有者可读写)
IFF_BAIDU_QIANFAN_KEY=***
IFF_VOLCENGINE_KEY=***
```

### ENV 转换算法

```python
def _env_key_for_provider(self, name: str) -> str:
    """Generate ENV var name for a provider."""
    return f"IFF_{name.upper().replace('-', '_')}_KEY"

def _serialize_providers(self):
    for name, p in self._providers.items():
        api_key = p.api_key or ""
        if api_key and not api_key.startswith("${"):
            env_var = p.key_env_var or self._env_key_for_provider(name)
            self._secrets.write(env_var, api_key)
            api_key = f"${{{env_var}}}"
        pd["api_key"] = api_key
        pd["key_env_var"] = p.key_env_var or self._env_key_for_provider(name)
```

### Proxy 启动注入

```python
# 在 CloudDiscovery.__init__ 或 proxy main() 中
secrets_path = Path.home() / ".inferfabric" / "secrets.env"
if secrets_path.exists():
    for line in secrets_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
```

### GET /admin/cloud/presets 响应格式

```json
{
  "presets": [
    {
      "id": "baidu-qianfan",
      "display_name": "百度千帆",
      "icon": "🟦",
      "openai_base": "https://...",
      "anthropic_base": "https://...",
      "env_var": "IFF_BAIDU_QIANFAN_KEY",
      "discovery": false,
      "model_count": 4
    }
  ]
}
```

### POST /admin/cloud/providers 扩展

```json
// 预设模式
{ "preset": "baidu-qianfan", "api_key": "sk-xxx" }

// 手动模式（向后兼容）
{ "name": "my-relay", "api_key": "sk-xxx", "openai_base": "https://..." }
```
