"""inferfabric/agent_registry.py — 客户端 Agent 识别引擎（Client as Plugin）。

与 cloud_discovery.py 对称：读 agents.d YAML → 归一化为 AgentHit。
Agent = 用请求头指纹识别的工具（Claude Code/Cursor/…），与 key_name（身份）
和 client IP（来源）是三个正交维度。

匹配语义（设计文档 §1）：
  - 文件按文件名排序，跨文件 first-match，任一 match 命中即归类
  - header 白名单 3 个信号；value 全等比较大小写不敏感；regex 用 re.search
  - protocol 可选条件（anthropic/openai），缺省不限
  - 单条规则坏（坏 regex / 未知 header / 非法 protocol / 空规则）→ 跳过该条
  - 单文件解析失败 → 跳过该文件（fail-open 于单文件）；reload 时意外异常 → 保留旧规则
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import yaml

log = logging.getLogger("inferfabric.agent_registry")

_HEADER_WHITELIST = ("x-app", "user-agent", "x-client-version")
_UNKNOWN_ID = "unknown"
_UNKNOWN_NAME = "未识别"
_UNKNOWN_COLOR = "#9ca3af"
_ROOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PROTOCOLS = ("anthropic", "openai")


def request_protocol(path: str) -> str:
    """从请求路径判定协议先验：/v1/messages → anthropic，其余 → openai。"""
    return "anthropic" if (path or "").startswith("/v1/messages") else "openai"


@dataclass(frozen=True)
class MatchRule:
    header: str | None = None
    value: str | None = None
    regex: str | None = None
    protocol: str | None = None
    _rx: re.Pattern | None = field(default=None, repr=False)

    @classmethod
    def build(cls, raw: dict) -> MatchRule | None:
        """从 YAML dict 构建；非法输入返回 None（调用方跳过该条）。"""
        header = raw.get("header")
        if header is not None and header.lower() not in _HEADER_WHITELIST:
            log.warning("agents.d: unknown header signal '%s' skipped", header)
            return None
        proto = raw.get("protocol")
        if proto is not None and proto not in _PROTOCOLS:
            log.warning("agents.d: invalid protocol '%s' skipped", proto)
            return None
        rx = None
        if raw.get("regex"):
            try:
                rx = re.compile(raw["regex"])
            except re.error as e:
                log.warning("agents.d: bad regex '%s' skipped (%s)", raw["regex"], e)
                return None
        rule = cls(
            header=header.lower() if header else None,
            value=raw.get("value"),
            regex=raw.get("regex"),
            protocol=proto,
            _rx=rx,
        )
        if rule.value is None and rule._rx is None and rule.protocol is None:
            return None
        return rule

    def matches(self, protocol: str, headers: Mapping) -> bool:
        if self.protocol is not None and self.protocol != protocol:
            return False
        if self.header is None:
            return True
        hv = ""
        # HTTP header 键大小写不敏感：aiohttp CIMultiDict 或普通 dict 统一扫描
        for k, val in headers.items():
            if str(k).lower() == self.header:
                hv = val
                break
        if self.value is not None:
            return hv.lower() == self.value.lower()
        if self._rx is not None:
            return self._rx.search(hv) is not None
        return False


@dataclass(frozen=True)
class AgentDef:
    id: str
    name: str
    color: str
    rules: tuple[MatchRule, ...]
    source: str  # "builtin" | "user"


@dataclass(frozen=True)
class _Snapshot:
    defs: tuple[AgentDef, ...]


@dataclass(frozen=True)
class AgentHit:
    agent: str
    name: str
    color: str
    ua: str


class AgentRegistry:
    """Agent 识别引擎。每请求 classify O(1)；热重载 fail-closed。"""

    def __init__(self, builtin_dir: Path, user_dir: Path):
        self._builtin_dir = Path(builtin_dir)
        self._user_dir = Path(user_dir)
        self._defs: list[AgentDef] = []
        self._snapshot: _Snapshot = _Snapshot(tuple())
        self.load()

    def load(self) -> None:
        try:
            self._defs = self._collect_defs()
        except Exception as e:
            log.error("AgentRegistry initial load failed — running with empty rules: %s", e)
            self._defs = []
        self._snapshot = _Snapshot(tuple(self._defs))
        log.info("AgentRegistry loaded: %d agents (builtin=%s user=%s)",
                 len(self._defs), self._builtin_dir, self._user_dir)

    def reload(self) -> bool:
        """热重载。收集过程抛异常（如权限错误）→ 保留旧快照，返回 False。"""
        try:
            new_defs = self._collect_defs()
        except Exception as e:
            log.error("AgentRegistry reload FAILED — keeping previous rules: %s", e)
            return False
        self._defs = new_defs
        self._snapshot = _Snapshot(tuple(new_defs))
        log.info("AgentRegistry reloaded: %d agents", len(new_defs))
        return True

    def classify(self, protocol: str, headers: Mapping) -> AgentHit:
        snap = self._snapshot
        ua = ""
        for k, val in headers.items():
            if str(k).lower() == "user-agent":
                ua = str(val or "")
                break
        ua = ua[:512]
        for d in snap.defs:
            for rule in d.rules:
                if rule.matches(protocol, headers):
                    return AgentHit(d.id, d.name, d.color, ua)
        return AgentHit(_UNKNOWN_ID, _UNKNOWN_NAME, _UNKNOWN_COLOR, ua)

    def all(self) -> list[AgentDef]:
        return list(self._defs)

    def policy(self, agent_id: str):  # noqa: ARG002 — 预留策略接缝，恒 None
        return None

    # ── internal ──

    def _collect_defs(self) -> list[AgentDef]:
        defs: list[AgentDef] = []
        index: dict[str, int] = {}
        for base, source in ((self._builtin_dir, "builtin"), (self._user_dir, "user")):
            if not base.is_dir():
                continue
            for path in sorted(base.glob("*.yaml")):
                d = self._read_def(path, source)
                if d is None:
                    continue
                idx = index.get(d.id)
                if idx is not None:
                    defs[idx] = d  # 用户目录覆盖内置（同 id）；保持内置原位置（排序稳定）
                else:
                    index[d.id] = len(defs)
                    defs.append(d)
        return defs

    def _read_def(self, path: Path, source: str) -> AgentDef | None:
        try:
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as e:
            log.error("agents.d: skip unreadable file %s (%s)", path, e)
            return None
        if not isinstance(raw, dict):
            log.warning("agents.d: %s is not a mapping, skipped", path)
            return None
        aid = raw.get("id")
        if not isinstance(aid, str) or not _ROOT_ID_RE.match(aid) or aid == _UNKNOWN_ID:
            log.warning("agents.d: %s has invalid/reserved id %r, skipped", path, aid)
            return None
        rules: list[MatchRule] = []
        for m in raw.get("match") or []:
            if isinstance(m, dict):
                rule = MatchRule.build(m)
                if rule is not None:
                    rules.append(rule)
        if not rules:
            log.warning("agents.d: %s has no usable rules, skipped", path)
            return None
        return AgentDef(
            id=aid,
            name=str(raw.get("name") or aid)[:40],
            color=str(raw.get("color") or _UNKNOWN_COLOR),
            rules=tuple(rules),
            source=source,
        )

    def add_from_ui(self, id: str, name: str, header: str,
                    pattern: str, color: str) -> AgentDef:
        """一键认领：校验 → 原子落盘用户目录 → reload → 返回新 def。

        Raises:
            ValueError: id/header/pattern 非法或为保留字
            KeyError:   id 已存在（新建语义）
        """
        if not _ROOT_ID_RE.match(id) or id == _UNKNOWN_ID:
            raise ValueError(f"invalid agent id: {id!r}")
        if header not in _HEADER_WHITELIST:
            raise ValueError(f"invalid header signal: {header!r}")
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"bad regex: {pattern!r} ({e})") from e
        if any(d.id == id for d in self._defs):
            raise KeyError(f"agent id already exists: {id}")
        self._user_dir.mkdir(parents=True, exist_ok=True)
        path = self._user_dir / f"{id}.yaml"
        agent = {"id": id, "name": name, "color": color,
                 "match": [{"header": header, "regex": pattern}]}
        comment = "# auto-generated {ts} (dashboard one-click) — 编辑后 iff reload 生效\n".format(
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(comment + yaml.safe_dump(agent, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        os.replace(tmp, path)  # 原子替换：不残留 .tmp，并发写最后者胜
        self.reload()
        return self._find(id)

    def remove(self, id: str) -> None:
        """删除用户目录 agent（管理模式）。内置/保留字/不存在 → 抛 ValueError。"""
        d = self._find(id)
        if d is None:
            raise ValueError(f"agent not found: {id}")
        if d.source != "user":
            raise ValueError(f"builtin agent cannot be removed: {id}")
        path = self._user_dir / f"{id}.yaml"
        if path.exists():
            path.unlink()
        self.reload()

    def _find(self, id: str) -> AgentDef | None:
        return next((d for d in self._defs if d.id == id), None)
