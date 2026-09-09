"""InferFabric 测试套件公共配置。

定义 pytest markers、共享 fixtures、目录结构。
"""

import pytest


def pytest_configure(config):
    """注册自定义 markers。"""
    config.addinivalue_line("markers", "unit: 纯单元测试，mock 所有外部依赖，无需 GPU/网络")
    config.addinivalue_line("markers", "integration: 集成测试，多组件交互，使用 mock HTTP server / SQLite")
    config.addinivalue_line("markers", "operational: 操作测试，操作真实环境（模型上下线、GPU 切换），需再确认后执行")


@pytest.fixture
def tmp_state_db(tmp_path):
    """创建临时 StateDB 实例。"""
    from inferfabric.state import StateDB
    db = StateDB(tmp_path / "state.db")
    return db


@pytest.fixture
def tmp_iffdb(tmp_path):
    """创建临时 IFFDB 实例。"""
    from inferfabric.db import IFFDB
    db = IFFDB(tmp_path / "iff.db")
    return db
