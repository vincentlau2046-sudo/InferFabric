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

class TestMergeAndBuiltins:
    """双目录合并：内置可加载、用户覆盖内置、坏文件跳过、reload fail-closed。"""

    def test_builtin_defaults_load(self, tmp_path):
        from inferfabric.agent_registry import AgentRegistry, _UNKNOWN_ID
        import inferfabric.agent_registry as ar
        builtin = Path(ar.__file__).parent / "agents.d"
        reg = AgentRegistry(builtin, tmp_path / "user")
        ids = {d.id for d in reg.all()}
        assert {"claude-code", "claude-desktop", "cursor", "codex"} <= ids

    def test_user_overrides_builtin_by_id(self, tmp_path):
        ov = """
id: claude-code
name: 我的自用
color: "#111111"
match:
  - { header: user-agent, regex: "^my-cc" }
"""
        b = tmp_path / "builtin"; b.mkdir()
        (b / "cc.yaml").write_text(CC, encoding="utf-8")
        u = tmp_path / "user"; u.mkdir()
        (u / "cc.yaml").write_text(ov, encoding="utf-8")
        reg = AgentRegistry(b, u)
        cc = [d for d in reg.all() if d.id == "claude-code"][0]
        assert cc.name == "我的自用" and cc.source == "user"
        # 覆盖后按新规则分类
        assert reg.classify("openai", {"User-Agent": "my-cc/1"}).agent == "claude-code"

    def test_broken_user_file_skipped_others_ok(self, tmp_path):
        b = tmp_path / "builtin"; b.mkdir()
        (b / "cc.yaml").write_text(CC, encoding="utf-8")
        u = tmp_path / "user"; u.mkdir()
        (u / "broken.yaml").write_text("{{{{ not yaml", encoding="utf-8")
        (u / "ok.yaml").write_text(CURSOR, encoding="utf-8")
        reg = AgentRegistry(b, u)
        ids = {d.id for d in reg.all()}
        assert "claude-code" in ids and "cursor" in ids   # 坏文件不影响好文件
        assert reg.classify("openai", {"User-Agent": "C/1"}).agent == _UNKNOWN_ID

class TestClaimAndManage:
    def test_claim_creates_file_and_reloads(self, tmp_path):
        r = _mk(tmp_path)
        d = r.add_from_ui("curl-cli", "curl-cli", "user-agent", "^curl", "#94a3b8")
        assert d.id == "curl-cli" and d.source == "user"
        # 落盘文件可读且内容吻合
        ua_file = tmp_path / "user" / "curl-cli.yaml"
        assert ua_file.exists()
        hit = r.classify("openai", {"User-Agent": "curl/8.5.2"})
        assert hit.agent == "curl-cli"

    def test_claim_no_tmp_leftover(self, tmp_path):
        r = _mk(tmp_path)
        r.add_from_ui("a1", "a1", "user-agent", "^a1", "#000")
        assert not list((tmp_path / "user").glob("*.tmp"))

    def test_claim_duplicate_raises_keyerror(self, tmp_path):
        r = _mk(tmp_path)
        r.add_from_ui("a1", "a1", "user-agent", "^a1", "#000")
        with pytest.raises(KeyError):
            r.add_from_ui("a1", "a1", "user-agent", "^a1", "#000")

    def test_claim_invalid_inputs(self, tmp_path):
        r = _mk(tmp_path)
        with pytest.raises(ValueError):
            r.add_from_ui("Bad ID!", "x", "user-agent", "^x", "#000")
        with pytest.raises(ValueError):
            r.add_from_ui("unknown", "x", "user-agent", "^x", "#000")   # 保留字
        with pytest.raises(ValueError):
            r.add_from_ui("ok", "x", "x-custom", "^x", "#000")          # header 白名单外
        with pytest.raises(ValueError):
            r.add_from_ui("ok", "x", "user-agent", "(", "#000")         # 坏 regex

    def test_remove_user_agent_allowed(self, tmp_path):
        r = _mk(tmp_path)
        r.add_from_ui("curl-cli", "curl-cli", "user-agent", "^curl", "#000")
        r.remove("curl-cli")
        assert r.classify("openai", {"User-Agent": "curl/8"}).agent == _UNKNOWN_ID
        assert not (tmp_path / "user" / "curl-cli.yaml").exists()

    def test_remove_builtin_rejected(self, tmp_path):
        b = tmp_path / "builtin"; b.mkdir()
        (b / "cc.yaml").write_text(CC, encoding="utf-8")
        r = AgentRegistry(b, tmp_path / "user")
        with pytest.raises(ValueError):
            r.remove("claude-code")

    def test_reload_failure_keeps_old(self, tmp_path, monkeypatch):
        r = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        before = r.classify("anthropic", {"x-app": "cli"}).agent
        def boom():
            raise OSError("disk lost")
        monkeypatch.setattr(r, "_collect_defs", boom)
        assert r.reload() is False
        assert r.classify("anthropic", {"x-app": "cli"}).agent == before


class TestClaimWriteLock:
    def test_add_from_ui_serialized_by_write_lock(self, tmp_path):
        """写路径（查重+落盘+reload）必须被 registry 写锁串行化：
        持锁时 add_from_ui 阻塞，放锁后完成，不残留 .tmp。"""
        import threading
        reg = _mk(tmp_path)
        lock = reg._write_lock
        lock.acquire()
        result = {}

        def worker():
            try:
                reg.add_from_ui("lock-tm", "lock-tm", "user-agent", r"^lock-tm", "#aabbcc")
                result["ok"] = True
            except Exception as e:  # noqa: BLE001
                result["err"] = repr(e)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(0.3)
        assert t.is_alive(), "持 _write_lock 时 add_from_ui 应阻塞（并发写需串行化）"
        lock.release()
        t.join(2.0)
        assert not t.is_alive()
        assert result.get("ok") is True, f"unexpected: {result}"
        assert not list((tmp_path / "user").glob("*.tmp")), ".tmp 半文件不应残留"


# ── v6.6 aliases：子进程工具归入已有 Agent（认领时选归属） ──

class TestAliases:
    def test_classify_matches_alias_rule(self, tmp_path):
        """agent 的 aliases 规则命中 → 归入该 agent（子进程工具归属）。"""
        reg = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        # 给 claude-code 加 alias：curl 归入它
        (tmp_path / "user" / "claude-code.yaml").write_text(
            "id: claude-code\naliases:\n  - { header: user-agent, regex: \"^curl\" }\n",
            encoding="utf-8")
        reg.reload()
        # 主规则仍命中
        assert reg.classify("anthropic", {"x-app": "cli"}).agent == "claude-code"
        # alias 命中 curl → 归入 claude-code（不是 unknown）
        assert reg.classify("openai", {"User-Agent": "curl/8.5.2"}).agent == "claude-code"

    def test_add_alias_to_builtin_writes_additive_override(self, tmp_path):
        """给 builtin agent 加 alias → 写 user-dir 覆盖文件（仅 aliases，additive），
        builtin 原 match 规则不丢。"""
        reg = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        d = reg.add_alias("claude-code", "user-agent", r"^curl")
        assert d is not None and d.id == "claude-code"
        # builtin 原规则仍工作
        assert reg.classify("anthropic", {"x-app": "cli"}).agent == "claude-code"
        # 新 alias 命中
        assert reg.classify("openai", {"User-Agent": "curl/8.5.2"}).agent == "claude-code"
        # 覆盖文件落盘
        override = tmp_path / "user" / "claude-code.yaml"
        assert override.exists()
        import yaml as _y
        raw = _y.safe_load(override.read_text(encoding="utf-8"))
        assert "aliases" in raw and raw["aliases"][0]["regex"] == "^curl"
        # 不含 match（additive，不复制 builtin 规则）
        assert "match" not in raw

    def test_add_alias_to_user_agent_appends(self, tmp_path):
        """给已有 user agent 追加 alias → 读改写，不丢已有 aliases。"""
        (tmp_path / "user").mkdir(exist_ok=True)
        (tmp_path / "user" / "myagent.yaml").write_text(
            "id: myagent\nname: My\nmatch:\n  - { header: user-agent, regex: \"^myagent\" }\n",
            encoding="utf-8")
        reg = _mk(tmp_path)
        reg.add_alias("myagent", "user-agent", r"^wget")
        assert reg.classify("openai", {"User-Agent": "wget/1.21"}).agent == "myagent"
        assert reg.classify("openai", {"User-Agent": "myagent/2"}).agent == "myagent"

    def test_add_alias_invalid_raises(self, tmp_path):
        reg = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        import pytest
        with pytest.raises(ValueError):
            reg.add_alias("claude-code", "bad-header", "^x")     # 非法 header
        with pytest.raises(ValueError):
            reg.add_alias("claude-code", "user-agent", "(unclosed")  # 坏 regex
        with pytest.raises(ValueError):
            reg.add_alias("nonexistent", "user-agent", "^x")     # 父 agent 不存在
