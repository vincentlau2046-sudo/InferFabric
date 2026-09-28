"""unit/proxy/test_agent_registry.py — Agent 识别引擎测试

覆盖 classify 规则矩阵：header 全等(value)/正则(regex)/协议条件/first-match
顺序/unknown 兜底/大小写不敏感/请求无 UA 头。
"""
from pathlib import Path
import pytest
from inferfabric.agent_registry import (
    AgentRegistry, MatchRule, AgentDef, AgentHit, request_protocol, _UNKNOWN_ID,
)

BUILTIN = None  # 用例内用 tmp_path 自建

def _mk(tmp_path, builtin=(), user=()):
    b = tmp_path / "builtin"; u = tmp_path / "user"
    for d in (b, u): d.mkdir(exist_ok=True)
    for name, body in builtin:
        (b / name).write_text(body, encoding="utf-8")
    for name, body in user:
        (u / name).write_text(body, encoding="utf-8")
    return AgentRegistry(b, u)

CC = """
id: claude-code
name: Claude Code
color: "#d97757"
match:
  - { header: x-app, value: cli }
  - { header: user-agent, regex: "^claude-cli/" }
"""
CURSOR = """
id: cursor
name: Cursor
color: "#000000"
match:
  - { header: user-agent, regex: "Cursor" }
"""

class TestClassify:
    def test_xapp_value_hit(self, tmp_path):
        r = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        hit = r.classify("anthropic", {"x-app": "cli"})
        assert hit.agent == "claude-code" and hit.name == "Claude Code"

    def test_xapp_value_case_insensitive(self, tmp_path):
        r = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        hit = r.classify("anthropic", {"X-APP": "CLI"})
        assert hit.agent == "claude-code"

    def test_ua_regex_search_hit(self, tmp_path):
        r = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        hit = r.classify("openai", {"User-Agent": "claude-cli/2.0.1"})
        assert hit.agent == "claude-code"

    def test_ua_regex_substring_not_anchor(self, tmp_path):
        r = _mk(tmp_path, builtin=[("cursor.yaml", CURSOR)])
        hit = r.classify("openai", {"User-Agent": "MyCursorApp/1.2"})
        assert hit.agent == "cursor"

    def test_protocol_condition_gates(self, tmp_path):
        only_openai = """
id: codex
name: Codex
color: "#000000"
match:
  - { protocol: openai }
"""
        r = _mk(tmp_path, builtin=[("codex.yaml", only_openai)])
        assert r.classify("openai", {}).agent == "codex"
        assert r.classify("anthropic", {}).agent == _UNKNOWN_ID

    def test_first_match_wins(self, tmp_path):
        # 两个 def 都命中同一条 UA → 排序在前者胜
        r = _mk(tmp_path, builtin=[("aa.yaml", CC), ("bb.yaml", CURSOR)])
        hit = r.classify("openai", {"User-Agent": "claude-cli/1.0 Cursor/2.0"})
        assert hit.agent == "claude-code"  # aa.yaml 排序在先

    def test_unknown_fallback_captures_ua(self, tmp_path):
        r = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        hit = r.classify("openai", {"User-Agent": "curl/8.5.2"})
        assert hit.agent == _UNKNOWN_ID and hit.ua == "curl/8.5.2"

    def test_no_ua_header(self, tmp_path):
        r = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        hit = r.classify("openai", {})
        assert hit.agent == _UNKNOWN_ID and hit.ua == ""

    def test_empty_dirs_all_unknown(self, tmp_path):
        r = _mk(tmp_path)
        hit = r.classify("openai", {"User-Agent": "curl/8"})
        assert hit.agent == _UNKNOWN_ID and hit.name == "未识别"

class TestMatchRuleBuild:
    def test_bad_regex_skipped(self):
        assert MatchRule.build({"header": "user-agent", "regex": "("}) is None

    def test_unknown_header_skipped(self):
        assert MatchRule.build({"header": "x-custom", "value": "x"}) is None

    def test_invalid_protocol_skipped(self):
        assert MatchRule.build({"protocol": "grpc"}) is None

    def test_empty_rule_skipped(self):
        assert MatchRule.build({"header": "user-agent"}) is None

    def test_valid_rule_built(self):
        r = MatchRule.build({"header": "x-app", "value": "cli"})
        assert r is not None and r.matches("anthropic", {"x-app": "cli"})

class TestRequestProtocol:
    def test_messages_anthropic(self):
        assert request_protocol("/v1/messages") == "anthropic"
    def test_chat_openai(self):
        assert request_protocol("/v1/chat/completions") == "openai"
