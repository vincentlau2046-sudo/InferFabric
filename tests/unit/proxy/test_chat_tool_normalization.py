"""
unit/proxy/test_chat_tool_normalization.py — Anthropic→OpenAI tools 转换回归测试

背景: Claude Code 通过 IFF 代理 /v1/chat/completions 发送 Anthropic 格式
tools（name/description/input_schema）。vLLM 的 OpenAI 端点要求
tools[i].function.parameters，否则逐工具 400（"Field required: function"）。
autocompact 后必现。本测试锁定 `_normalize_tools_for_openai` 的转换行为。
"""

import copy
import pytest

from inferfabric.proxy.chat_handlers import _normalize_tools_for_openai


# ─── 测试数据 ───

ANTHROPIC_TOOL = {
    "name": "Read",
    "description": "Read a file",
    "input_schema": {
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
    },
}

OPENAI_TOOL = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": "Read a file",
        "parameters": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
    },
}


class TestNormalizeTools:
    def test_anthropic_tool_converted(self):
        """input_schema 工具 → function.parameters（vLLM 可接受）。"""
        data = {"model": "qwen38-27b-abliterated",
                 "messages": [{"role": "user", "content": "hi"}],
                 "tools": [dict(ANTHROPIC_TOOL)]}
        _normalize_tools_for_openai(data)
        assert data["tools"][0] == OPENAI_TOOL

    def test_openai_tool_untouched(self):
        """已是 OpenAI 格式（带 function）的工具保持不变。"""
        data = {"tools": [copy.deepcopy(OPENAI_TOOL)]}
        snapshot = copy.deepcopy(data["tools"])
        _normalize_tools_for_openai(data)
        assert data["tools"] == snapshot

    def test_mixed_tools_only_anthropic_converted(self):
        """混合列表中只转换含 input_schema 的工具。"""
        anthropic = dict(ANTHROPIC_TOOL)
        data = {"tools": [anthropic, copy.deepcopy(OPENAI_TOOL)]}
        _normalize_tools_for_openai(data)
        assert data["tools"][0] == OPENAI_TOOL
        assert data["tools"][1]["function"]["name"] == "Read"

    def test_missing_input_schema_defaults_empty_object(self):
        """input_schema 缺失/为 None 时 parameters 默认为空 object schema。"""
        data = {"tools": [{"name": "Bash", "description": "run command", "input_schema": None}]}
        _normalize_tools_for_openai(data)
        assert data["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}

    def test_no_tools_noop(self):
        """无 tools 字段不报错。"""
        data = {"model": "m", "messages": []}
        _normalize_tools_for_openai(data)
        assert "tools" not in data

    def test_tools_not_list_ignored(self):
        """tools 非 list（如 None/str）时不做转换。"""
        for bad in (None, "x", {"name": "Bash"}):
            data = {"tools": bad}
            _normalize_tools_for_openai(data)  # 不应抛错
            assert data["tools"] == bad


class TestNormalizeToolChoice:
    def test_tool_choice_tool_name(self):
        """Anthropic {"type":"tool","name":X} → OpenAI function 对象。"""
        data = {"tool_choice": {"type": "tool", "name": "Read"}}
        _normalize_tools_for_openai(data)
        assert data["tool_choice"] == {"type": "function", "function": {"name": "Read"}}

    def test_tool_choice_any(self):
        """{"type":"any"} → "required"。"""
        data = {"tool_choice": {"type": "any"}}
        _normalize_tools_for_openai(data)
        assert data["tool_choice"] == "required"

    def test_tool_choice_auto(self):
        """{"type":"auto"} → "auto"。"""
        data = {"tool_choice": {"type": "auto"}}
        _normalize_tools_for_openai(data)
        assert data["tool_choice"] == "auto"

    def test_tool_choice_none(self):
        """{"type":"none"} → "none"。"""
        data = {"tool_choice": {"type": "none"}}
        _normalize_tools_for_openai(data)
        assert data["tool_choice"] == "none"

    def test_tool_choice_openai_string_untouched(self):
        """已是 OpenAI 字符串形式保持不变。"""
        for s in ("auto", "required", "none"):
            data = {"tool_choice": s}
            _normalize_tools_for_openai(data)
            assert data["tool_choice"] == s

    def test_tool_choice_openai_function_object_untouched(self):
        """OpenAI function 对象（type=function）保持不变。"""
        tc = {"type": "function", "function": {"name": "Read"}}
        data = {"tool_choice": tc}
        _normalize_tools_for_openai(data)
        assert data["tool_choice"] == tc
