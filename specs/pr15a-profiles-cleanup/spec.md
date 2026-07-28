# PR-15a: 删 Profile 死代码 + /profiles deprecation

## 变更范围
- `config.py`: 删 class Profile (L280-287) + load_profiles() (L422-441)
- `manager.py`: 删 Profile, load_profiles import (L27-28)
- `profile_manager.py`: 整文件删除
- `__init__.py`: 删 ProfileManager 导出，保留 ProfileManager = ModelManager 别名 (deprecated)
- `handler.py`: /profiles 路由加 log.warning("deprecated")

## 不动
- state.py ProfileState — 活代码，15+ 处引用
- manager.py ProfileManager class — 保留为 ModelManager 别名

## 风险
- 遗漏 import → ImportError → proxy 起不来 → 三遍 grep 验证
- /profiles 消费方 → 保留路由 + warning，不 301
