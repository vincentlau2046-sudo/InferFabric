# Task Breakdown — v47-cloud-presets

## 依赖图

```
T1 (presets + SecretsManager)
 ├── T2 (handler API)
 │    └── T3 (Dashboard UI)
 └── T4 (集成测试)
      └── T5 (Review + 合入)
```

## 任务列表

### T1: cloud_presets.yaml + SecretsManager + ENV 转换
- **依赖**: 无
- **文件**: `cloud_presets.yaml`(新), `cloud_discovery.py`(改)
- **验收**:
  - [ ] `load_presets()` 返回 9 个预设
  - [ ] `SecretsManager` 可读写 secrets.env
  - [ ] `_serialize_providers()` 明文 key → ENV 转换
  - [ ] proxy 启动注入 secrets.env
  - [ ] 现有 `${VAR}` 格式不受影响

### T2: handler.py API 端点
- **依赖**: T1
- **文件**: `proxy/handler.py`(改)
- **验收**:
  - [ ] `GET /admin/cloud/presets` 返回预设列表
  - [ ] `POST /admin/cloud/providers` 支持 `preset` 字段
  - [ ] `GET /admin/cloud/providers` 返回 `key_env_var` + `key_env_set`
  - [ ] 向后兼容：无 preset 字段时走手动模式

### T3: Dashboard UI
- **依赖**: T2
- **文件**: `cloud.html`(改), `app.js`(改)
- **验收**:
  - [ ] 预设卡片网格渲染
  - [ ] 选预设→预填 base URL→只填 Key
  - [ ] Provider 列表展示 ENV + ✅/⚠️ 状态
  - [ ] 手动模式保留

### T4: 集成测试
- **依赖**: T1
- **文件**: `tests/test_cloud_presets.py`(新)
- **验收**:
  - [ ] 预设加载测试
  - [ ] 添加 Provider（预设+手动）
  - [ ] ENV 转换 + secrets.env 读写
  - [ ] Key 状态查询

### T5: OpenCode Review + 生产合入
- **依赖**: T1-T4 全部完成
- **流程**:
  1. OpenCode review 全部改动
  2. 有问题 → 修复 → 重新 review
  3. review 通过 → diff 生产 → cp 合入
  4. 重启 proxy 验证
