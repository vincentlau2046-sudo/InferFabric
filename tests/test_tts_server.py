"""
tests/test_tts_server.py — TTS server type tests for IFF v4.7.0 PR-B.

Covers: TTSConfig, load_models tts_server parsing, ModelConfig properties,
start/stop process management (mocked), health check, state management.
"""

import json
import os
import signal
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ─── TTSConfig Unit Tests ────────────────────────────────────────

def test_tts_config_defaults():
    """B-T1: TTSConfig dataclass construction with defaults."""
    from inferfabric.config import TTSConfig

    cfg = TTSConfig(conda_env="qwen3-tts")
    assert cfg.conda_env == "qwen3-tts"
    assert cfg.port == 8880
    assert cfg.working_dir == ""
    assert cfg.health_url == ""
    assert cfg.health_check_timeout == 180
    assert cfg.start_cmd == "python -m api.main"
    assert cfg.extra_env == {}


def test_tts_config_full():
    """B-T1: TTSConfig with all fields specified."""
    from inferfabric.config import TTSConfig

    cfg = TTSConfig(
        conda_env="qwen3-tts",
        port=9999,
        working_dir="~/services/tts",
        health_url="http://localhost:9999/health",
        health_check_timeout=300,
        start_cmd="python -m api.main --debug",
        extra_env={"TTS_BACKEND": "official", "TTS_LAZY_LOAD": "false"},
    )
    assert cfg.port == 9999
    assert cfg.health_check_timeout == 300
    assert cfg.extra_env["TTS_BACKEND"] == "official"


def test_tts_config_resolved_working_dir_explicit():
    """B-T1: resolved_working_dir with explicit path."""
    from inferfabric.config import TTSConfig

    cfg = TTSConfig(conda_env="qwen3-tts", working_dir="~/services/Qwen3-TTS-Openai-Fastapi")
    resolved = cfg.resolved_working_dir
    assert "Qwen3-TTS-Openai-Fastapi" in str(resolved)
    assert resolved.is_absolute()


def test_tts_config_resolved_working_dir_default():
    """B-T1: resolved_working_dir falls back to ~/services/tts."""
    from inferfabric.config import TTSConfig

    cfg = TTSConfig(conda_env="qwen3-tts")
    resolved = cfg.resolved_working_dir
    assert str(resolved).endswith("services/tts")


# ─── load_models() tts_server Parsing ────────────────────────────

def test_load_models_tts_server():
    """B-T2: load_models() parses tts_server nested fields."""
    from inferfabric.config import load_models

    models = load_models()
    tts = models.get("tts-qwen3")
    assert tts is not None, "tts-qwen3 not found in loaded models"
    assert tts.type == "tts_server"
    assert tts.tts is not None
    assert tts.tts.conda_env == "qwen3-tts"
    assert tts.tts.port == 8880
    assert tts.tts.health_url == "http://localhost:8880/health"
    assert tts.tts.health_check_timeout == 180


def test_load_models_tts_extra_env():
    """B-T2: load_models() injects extra_env from tts_server config."""
    from inferfabric.config import load_models

    models = load_models()
    tts = models.get("tts-qwen3")
    assert tts.tts.extra_env["TTS_BACKEND"] == "official"
    assert tts.tts.extra_env["TTS_LAZY_LOAD"] == "false"
    assert tts.tts.extra_env["TTS_WARMUP_ON_START"] == "true"


def test_load_models_tts_extra_env_protected():
    """B-T2: extra_env protected key raises ConfigError."""
    from inferfabric.config import load_models, ConfigError

    with tempfile.TemporaryDirectory() as tmpdir:
        models_dir = Path(tmpdir)
        yaml_content = """
name: tts-bad
type: tts_server
gpu_role: shared
tts_server:
  conda_env: test
  extra_env:
    PATH: /malicious/path
"""
        (models_dir / "tts-bad.yaml").write_text(yaml_content)
        with pytest.raises(ConfigError, match="protected"):
            load_models(models_dir)


def test_load_models_tts_top_level_fallback():
    """B-T2: tts_server top-level fields parsed when no nested block."""
    from inferfabric.config import load_models

    with tempfile.TemporaryDirectory() as tmpdir:
        models_dir = Path(tmpdir)
        yaml_content = """
name: tts-simple
type: tts_server
gpu_role: shared
conda_env: test-env
port: 9999
start_cmd: python server.py
"""
        (models_dir / "tts-simple.yaml").write_text(yaml_content)
        models = load_models(models_dir)
        m = models.get("tts-simple")
        assert m is not None
        assert m.tts is not None
        assert m.tts.conda_env == "test-env"
        assert m.tts.port == 9999


# ─── ModelConfig Properties ──────────────────────────────────────

def test_model_config_is_tts_server():
    """B-T3: is_tts_server property."""
    from inferfabric.config import ModelConfig, TTSConfig

    m = ModelConfig(
        name="tts-test",
        description="test",
        type="tts_server",
        tts=TTSConfig(conda_env="test"),
    )
    assert m.is_tts_server is True

    m2 = ModelConfig(name="vllm-test", description="test", type="vllm")
    assert m2.is_tts_server is False


def test_model_config_tts_port():
    """B-T3: port property returns tts.port for tts_server."""
    from inferfabric.config import ModelConfig, TTSConfig

    m = ModelConfig(
        name="tts-test",
        description="test",
        type="tts_server",
        tts=TTSConfig(conda_env="test", port=9999),
    )
    assert m.port == 9999


def test_model_config_tts_served_name():
    """B-T3: served_name for tts_server returns self.name."""
    from inferfabric.config import ModelConfig, TTSConfig

    m = ModelConfig(
        name="tts-test",
        description="test",
        type="tts_server",
        tts=TTSConfig(conda_env="test"),
    )
    assert m.served_name == "tts-test"


def test_model_config_tts_modality():
    """B-T3: resolved_modality for tts model_type."""
    from inferfabric.config import ModelConfig, TTSConfig

    m = ModelConfig(
        name="tts-test",
        description="test",
        type="tts_server",
        model_type="tts",
        tts=TTSConfig(conda_env="test"),
    )
    assert m.resolved_modality == "tts"


# ─── Process Management (Mocked) ─────────────────────────────────

def test_start_tts_server_cmd_build():
    """B-T4: start_tts_server shlex.split + python replacement."""
    from inferfabric.config import TTSConfig

    cfg = TTSConfig(conda_env="qwen3-tts", start_cmd="python -m api.main --debug")
    import shlex
    cmd = shlex.split(cfg.start_cmd)
    assert cmd[0] == "python"
    assert cmd[1] == "-m"
    assert cmd[2] == "api.main"
    assert "--debug" in cmd


def test_start_tts_server_extra_env_injection():
    """B-T5: start_tts_server injects extra_env into subprocess env."""
    from inferfabric.process_manager import ProcessManager
    from inferfabric.config import TTSConfig
    from inferfabric.state import StateDB

    with tempfile.TemporaryDirectory() as tmpdir:
        state = StateDB(Path(tmpdir) / "state.db")
        pm = ProcessManager(state, log_dir=Path(tmpdir))

        cfg = TTSConfig(
            conda_env="qwen3-tts",
            working_dir=tmpdir,  # use existing dir
            extra_env={"TTS_BACKEND": "official", "TTS_LAZY_LOAD": "false"},
        )

        with patch("inferfabric.process_manager.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            with patch("inferfabric.process_manager.wait_http", return_value=True):
                with patch("builtins.open", MagicMock()):
                    result = pm.start_tts_server(cfg)

            # Verify Popen was called
            assert mock_popen.called
            # Verify env was modified
            call_args = mock_popen.call_args
            env = call_args[1].get("env") if call_args[1] else None
            assert env is not None
            assert env["TTS_BACKEND"] == "official"
            assert env["TTS_LAZY_LOAD"] == "false"


def test_start_tts_server_process_group():
    """B-T6: start_tts_server uses start_new_session + records tts_pid."""
    from inferfabric.process_manager import ProcessManager
    from inferfabric.config import TTSConfig
    from inferfabric.state import StateDB

    with tempfile.TemporaryDirectory() as tmpdir:
        state = StateDB(Path(tmpdir) / "state.db")
        pm = ProcessManager(state, log_dir=Path(tmpdir))

        cfg = TTSConfig(conda_env="qwen3-tts", working_dir=tmpdir)

        with patch("inferfabric.process_manager.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 54321
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            with patch("inferfabric.process_manager.wait_http", return_value=True):
                with patch("builtins.open", MagicMock()):
                    result = pm.start_tts_server(cfg)

            # Verify start_new_session=True
            call_args = mock_popen.call_args
            assert call_args[1].get("start_new_session") is True

        # Verify tts_pid recorded
        assert pm.tts_pid == 54321
        assert result["status"] == "healthy"


def test_stop_tts_server_graceful():
    """B-T7: stop_tts_server SIGTERM → graceful → PID cleared."""
    from inferfabric.process_manager import ProcessManager
    from inferfabric.state import StateDB

    with tempfile.TemporaryDirectory() as tmpdir:
        state = StateDB(Path(tmpdir) / "state.db")
        state.set("tts_pid", "54321")
        pm = ProcessManager(state, log_dir=Path(tmpdir))

        with patch("os.killpg") as mock_killpg:
            # First call (SIGTERM) succeeds, second (check) raises ProcessLookupError
            mock_killpg.side_effect = [None, ProcessLookupError()]
            with patch.object(pm, "_reap_zombies"):
                with patch.object(pm, "_wait_gpu_idle", return_value={"status": "ok"}):
                    result = pm.stop_tts_server(port=8880)

        assert result["status"] == "ok"
        assert pm.tts_pid is None  # PID cleared


def test_stop_tts_server_sigkill():
    """B-T7: stop_tts_server SIGTERM timeout → SIGKILL."""
    from inferfabric.process_manager import ProcessManager
    from inferfabric.state import StateDB

    with tempfile.TemporaryDirectory() as tmpdir:
        state = StateDB(Path(tmpdir) / "state.db")
        state.set("tts_pid", "54321")
        pm = ProcessManager(state, log_dir=Path(tmpdir))

        killpg_calls = []

        def mock_killpg(pgid, sig):
            killpg_calls.append((pgid, sig))
            # SIGTERM check always succeeds (process alive) until SIGKILL
            if sig == 0:
                return  # process still alive
            if sig == signal.SIGKILL:
                return  # killed

        with patch("os.killpg", side_effect=mock_killpg):
            with patch.object(pm, "_validate_pid", return_value=True):
                # SIGTERM check (sig=0) never raises → timeout → SIGKILL
                with patch.object(pm, "_reap_zombies"):
                    with patch.object(pm, "_wait_gpu_idle", return_value={"status": "ok"}):
                        result = pm.stop_tts_server(port=8880)

        assert result["status"] == "ok"
        assert "killed" in result["message"].lower()


# ─── Lifecycle Dispatch ───────────────────────────────────────────

def test_start_model_tts_dispatch():
    """B-T8: _start_model dispatches to start_tts_server."""
    from inferfabric.model_lifecycle import ModelLifecycle
    from inferfabric.config import ModelConfig, TTSConfig

    mock_state = MagicMock()
    mock_proc = MagicMock()
    mock_health = MagicMock()
    mock_lock = MagicMock()
    mock_gpu = MagicMock()

    lc = ModelLifecycle(mock_state, mock_proc, mock_health, mock_lock, mock_gpu, {})

    model = ModelConfig(
        name="tts-test",
        description="test",
        type="tts_server",
        tts=TTSConfig(conda_env="test"),
    )
    lc._start_model(model)
    mock_proc.start_tts_server.assert_called_once_with(model.tts)


def test_stop_model_process_tts():
    """_stop_model_process dispatches to stop_tts_server."""
    from inferfabric.model_lifecycle import ModelLifecycle
    from inferfabric.config import ModelConfig, TTSConfig

    mock_state = MagicMock()
    mock_proc = MagicMock()
    mock_health = MagicMock()
    mock_lock = MagicMock()
    mock_gpu = MagicMock()

    lc = ModelLifecycle(mock_state, mock_proc, mock_health, mock_lock, mock_gpu, {})

    model = ModelConfig(
        name="tts-test",
        description="test",
        type="tts_server",
        tts=TTSConfig(conda_env="test", port=8880),
    )
    lc._stop_model_process(model, "tts-test")
    mock_proc.stop_tts_server.assert_called_once_with(port=8880)


# ─── Health Check ─────────────────────────────────────────────────

def test_health_check_tts_server():
    """B-T11: check_model_health for tts_server returns correct status."""
    from inferfabric.health_checker import check_model_health
    from inferfabric.config import ModelConfig, TTSConfig

    model = ModelConfig(
        name="tts-test",
        description="test",
        type="tts_server",
        tts=TTSConfig(conda_env="test", health_url="http://localhost:8880/health"),
    )

    with patch("inferfabric.health_checker.check_http_status", return_value="✅"):
        result = check_model_health(model)
        assert result == "✅"


def test_health_check_tts_default_url():
    """B-T11: tts_server health URL defaults to localhost:port/health."""
    from inferfabric.health_checker import check_model_health
    from inferfabric.config import ModelConfig, TTSConfig

    model = ModelConfig(
        name="tts-test",
        description="test",
        type="tts_server",
        tts=TTSConfig(conda_env="test", port=9999),  # no health_url
    )

    with patch("inferfabric.health_checker.check_http_status", return_value="⏳") as mock_check:
        result = check_model_health(model)
        # Verify it was called with the default URL
        mock_check.assert_called_once_with("http://localhost:9999/health")


# ─── State Management ────────────────────────────────────────────

def test_tts_pid_read_write():
    """tts_pid reads/writes correctly from state.db."""
    from inferfabric.process_manager import ProcessManager
    from inferfabric.state import StateDB

    with tempfile.TemporaryDirectory() as tmpdir:
        state = StateDB(Path(tmpdir) / "state.db")
        pm = ProcessManager(state, log_dir=Path(tmpdir))

        assert pm.tts_pid is None

        pm._set_tts_pid(12345)
        assert pm.tts_pid == 12345

        pm._set_tts_pid(None)
        assert pm.tts_pid is None


# ─── Config Hash ─────────────────────────────────────────────────

def test_config_hash_tts():
    """B-T8: config_hash includes TTS config for drift detection."""
    from inferfabric.config import ModelConfig, TTSConfig

    m1 = ModelConfig(
        name="tts-test",
        description="test",
        type="tts_server",
        tts=TTSConfig(conda_env="test", port=8880),
    )
    m2 = ModelConfig(
        name="tts-test",
        description="test",
        type="tts_server",
        tts=TTSConfig(conda_env="test", port=9999),
    )
    assert m1.config_hash() != m2.config_hash()


def test_config_hash_tts_extra_env():
    """config_hash changes when extra_env changes."""
    from inferfabric.config import ModelConfig, TTSConfig

    m1 = ModelConfig(
        name="tts-test",
        description="test",
        type="tts_server",
        tts=TTSConfig(conda_env="test", extra_env={"KEY": "val1"}),
    )
    m2 = ModelConfig(
        name="tts-test",
        description="test",
        type="tts_server",
        tts=TTSConfig(conda_env="test", extra_env={"KEY": "val2"}),
    )
    assert m1.config_hash() != m2.config_hash()


# ─── PR-A Regression Tests (A-T8, A-T9) ──────────────────────────

def test_old_served_name_not_found():
    """A-T7: Old served_names return 404 after rename."""
    from inferfabric.config import load_models

    models = load_models()
    old_names = ["vllm_qw35_gptq", "vllm_qwen36_35b", "vllm_qwen27b_vl", "gemma-4-31B-it-NVFP4"]
    for old_name in old_names:
        # find_model_by_served_name equivalent: check if any model has this served_name
        found = any(m.served_name == old_name for m in models.values())
        assert not found, f"Old served_name {old_name} still exists"


def test_exception_models_unchanged():
    """A-T8: Exception models (bge-m3, ovis-ocr2) are correctly registered."""
    from inferfabric.config import load_models

    models = load_models()
    assert "bge-m3" in models
    assert models["bge-m3"].served_name == "bge-m3"
    assert "ovis-ocr2" in models
    assert models["ovis-ocr2"].served_name == "ovis-ocr2"


def test_name_equals_served_name():
    """A-T9: All models have name == served_name (IFF naming rule)."""
    from inferfabric.config import load_models

    models = load_models()
    for name, m in models.items():
        # vllm models have explicit served_name; others use self.name
        if m.vllm:
            assert m.vllm.served_name == name, \
                f"vllm model {name}: served_name={m.vllm.served_name} != name={name}"
        else:
            assert m.served_name == name, \
                f"non-vllm model {name}: served_name={m.served_name} != name={name}"
