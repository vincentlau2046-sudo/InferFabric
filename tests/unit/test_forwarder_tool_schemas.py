"""Test _ensure_tool_schemas — normalizes missing input_schema on cloud Anthropic tools."""

from inferfabric.forwarder import _ensure_tool_schemas


def test_no_tools():
    """No tools key → no-op."""
    data = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}]}
    _ensure_tool_schemas(data)
    assert "tools" not in data


def test_empty_tools_list():
    """Empty tools list → no-op."""
    data = {"tools": []}
    _ensure_tool_schemas(data)
    assert data["tools"] == []


def test_skips_tools_with_input_schema():
    """Tools with input_schema already present are left untouched."""
    data = {
        "tools": [
            {
                "name": "get_weather",
                "description": "Get current weather",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
            }
        ]
    }
    _ensure_tool_schemas(data)
    tools = data["tools"]
    assert tools[0]["name"] == "get_weather"
    assert tools[0]["input_schema"] == {"type": "object", "properties": {"city": {"type": "string"}}}


def test_injects_default_schema_for_server_tool():
    """Server-side tool (web_search) missing input_schema gets a minimal default."""
    data = {
        "tools": [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}
        ]
    }
    _ensure_tool_schemas(data)
    assert data["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
    # Other fields preserved
    assert data["tools"][0]["name"] == "web_search"
    assert data["tools"][0]["max_uses"] == 8


def test_tool_has_type_but_no_input_schema():
    """Tool with 'type' but no input_schema gets default."""
    data = {
        "tools": [
            {"type": "computer_20250124", "name": "computer"}
        ]
    }
    _ensure_tool_schemas(data)
    assert data["tools"][0]["input_schema"] == {"type": "object", "properties": {}}


def test_mixed_tools():
    """Server tool missing input_schema gets injected; regular tool with schema untouched."""
    data = {
        "tools": [
            {"name": "get_weather", "description": "Get weather", "input_schema": {"type": "object"}},
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
        ]
    }
    _ensure_tool_schemas(data)
    assert data["tools"][0]["input_schema"] == {"type": "object"}  # untouched
    assert data["tools"][1]["input_schema"] == {"type": "object", "properties": {}}  # injected


def test_non_dict_tool_unaffected():
    """Non-dict entries in tools are skipped."""
    data = {"tools": ["not_a_dict", None]}
    _ensure_tool_schemas(data)  # no crash


def test_tools_not_a_list():
    """tools key is not a list → no-op."""
    data = {"tools": "not_a_list"}
    _ensure_tool_schemas(data)
    assert data["tools"] == "not_a_list"