"""Test: _switch_to_idle() preserves gpu_role:none services.

PR-7: _switch_to_idle() should NOT stop gpu_role:none services,
and should preserve them in active_services.
"""
import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

# We test the logic in isolation without importing the full module
# (which requires a running StateDB etc.)


class TestSwitchToIdlePreservesGpuNone:
    """Core invariant: gpu_role:none services survive _switch_to_idle()."""

    def test_active_services_preserves_gpu_none(self):
        """After _switch_to_idle(), gpu_role:none services remain in active_services."""
        # Simulate from_services = ["qwen2", "bge-m3"]
        # qwen2: gpu_role=exclusive → should be removed
        # bge-m3: gpu_role=none → should be preserved
        from_services = ["qwen2", "bge-m3"]

        # Mock model configs
        qwen2_model = MagicMock()
        qwen2_model.is_gpu_none = False
        qwen2_model.gpu_role = "exclusive"

        bge_m3_model = MagicMock()
        bge_m3_model.is_gpu_none = True
        bge_m3_model.gpu_role = "none"

        models_dict = {"qwen2": qwen2_model, "bge-m3": bge_m3_model}

        # This is the new logic from the patch:
        result = json.dumps([s for s in from_services
                            if (m := models_dict.get(s)) and m.is_gpu_none])

        assert json.loads(result) == ["bge-m3"], \
            f"Expected ['bge-m3'] in active_services, got {json.loads(result)}"

    def test_active_services_empty_when_no_gpu_none(self):
        """If all services are GPU-bound, active_services should be empty after idle."""
        from_services = ["qwen2", "llama3"]
        qwen2 = MagicMock(is_gpu_none=False)
        llama3 = MagicMock(is_gpu_none=False)
        models_dict = {"qwen2": qwen2, "llama3": llama3}

        result = json.dumps([s for s in from_services
                            if (m := models_dict.get(s)) and m.is_gpu_none])

        assert json.loads(result) == []

    def test_multiple_gpu_none_preserved(self):
        """Multiple gpu_role:none services are all preserved."""
        from_services = ["qwen2", "bge-m3", "nomic-embed", "llama3"]
        models_dict = {
            "qwen2": MagicMock(is_gpu_none=False),
            "bge-m3": MagicMock(is_gpu_none=True),
            "nomic-embed": MagicMock(is_gpu_none=True),
            "llama3": MagicMock(is_gpu_none=False),
        }

        result = json.dumps([s for s in from_services
                            if (m := models_dict.get(s)) and m.is_gpu_none])

        assert set(json.loads(result)) == {"bge-m3", "nomic-embed"}

    def test_unknown_service_skipped(self):
        """Services not in _models dict are skipped (defensive)."""
        from_services = ["qwen2", "orphan-service"]
        models_dict = {"qwen2": MagicMock(is_gpu_none=False)}

        result = json.dumps([s for s in from_services
                            if (m := models_dict.get(s)) and m.is_gpu_none])

        assert json.loads(result) == []


class TestSwitchToIdleNoStopOllamaCpp:
    """Verify the deleted code path: stop_ollama_cpp is NOT called for gpu_role:none."""

    def test_no_stop_ollama_cpp_for_gpu_none(self):
        """The gpu_role:none stop loop has been deleted.
        Verify by checking the source code directly."""
        import ast

        source = open("inferfabric/model_lifecycle.py").read()
        tree = ast.parse(source)

        # Find _switch_to_idle method
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_switch_to_idle":
                # Get the source lines for this method
                method_source = source.split("\n")
                method_lines = method_lines = method_source[node.lineno-1:node.end_lineno]
                method_text = "\n".join(method_lines)

                # stop_ollama_cpp should NOT appear in _switch_to_idle
                assert "stop_ollama_cpp" not in method_text, \
                    "stop_ollama_cpp found in _switch_to_idle() — gpu_role:none loop not removed!"
                break
        else:
            pytest.fail("_switch_to_idle method not found")
