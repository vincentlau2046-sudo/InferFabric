# CLI Audit Report

## 审计日期
2026-07-12

## 脚本清单

| 脚本 | 操作 | 理由 |
|------|------|------|
| `switch_vllm.sh.bak` | **删除** | 备份文件，功能已被 `iff switch` 取代 |
| `download-vlm.sh` | **标记 DEPRECATED** | 脚本功能正常，但已被 `iff download` 取代 |
| `switch_comfyui.sh` | **标记 DEPRECATED** | 薄封装 `iff switch comfyui` / `iff stop comfyui`，已被 iff 直接取代 |
| `iff-recovery.sh` | **修复死引用** | 第 1 步 fallback 中引用 `switch_comfyui.sh stop`，改为 `iff stop comfyui`；更新注释反映废弃状态 |

## 决策记录

### 1. `switch_vllm.sh.bak` — 删除
- 该文件是 `.bak` 备份，原脚本 `switch_vllm.sh` 的旧版本
- 功能已完全被 `iff switch <model_name>` 取代
- **操作**: 直接删除

### 2. `download-vlm.sh` — 保留并标记 DEPRECATED
- 脚本功能完整可用，仅用于下载特定 VLM 模型 (Huihui-Qwen3.6-27B)
- CLI 已提供 `iff download` 命令替代
- 保留文件以供参考，在头部添加 DEPRECATED 注释和替代方案说明
- **操作**: 文件头部添加 DEPRECATED 标记，保留文件

### 3. `switch_comfyui.sh` — 保留并标记 DEPRECATED
- 薄封装，仅包装 `iff switch comfyui` / `iff stop comfyui` / `iff status`
- 无额外逻辑，完全被 iff 取代
- 保留文件以供旧工作流参考
- **操作**: 文件头部添加 DEPRECATED 标记，保留文件

### 4. `iff-recovery.sh` — 修复死引用
- 发现第 1 步 fallback 分支中调用 `~/inferfabric/scripts/switch_comfyui.sh stop`
- 该脚本已废弃且功能已被 iff 取代
- 将调用改为 `iff stop comfyui`
- 更新注释说明 `switch_vllm.sh 已废弃 (removed)` 和 `switch_comfyui.sh 已废弃`
- **操作**: 替换死引用，更新注释

## 后续建议
- 所有新工作流应直接使用 `iff` CLI 命令
- 下一阶段可考虑移除所有 DEPRECATED 脚本
- `iff-recovery.sh` 保留为紧急恢复工具，其核心逻辑（GPU 重置、锁清理、state DB 重建）尚未被 iff 内建命令完全覆盖