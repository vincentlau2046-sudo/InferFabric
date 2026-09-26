"""iff tune — 场景预设调优编排器（引擎无关，三层分离）。

三层分离：
  1. 场景定义 models.d/scenarios.yaml（侧车，手动配置/可 git 审；同名场景覆盖
     模型 YAML 内联 `presets:`）
  2. 应用层 ~/.inferfabric/active_scenarios.yaml（机器本地状态，只由 tune 写入：
     {model: {active_preset, overrides}}；load_models overlay 合并进引擎块）
  3. 模型 YAML = 启动真相源，tune 工具链只读（注释永不被工具链破坏）

default = 清除该模型应用层条目 → 启动直接落回纯 model.yaml 值——
"应用前原点"就是 YAML 本身，无需基线快照 / YAML 备份。
重启失败 / MTP 冒烟失败 → 还原上一次应用层条目 + 旧配置重启。

本模块只依赖 ModelConfig + EngineAdapter 的可选钩子（scenario_fields / engine_caps /
validate_scenario / restart），**不 import 任何具体引擎**——未来 vLLM/SGLang 实现
适配器钩子即可接入，CLI 与 /admin/tune 流程零改动。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import yaml

from .config import APPLIED_SCENARIOS_FILE, load_models

log = logging.getLogger("inferfabric.tune")

# 应用层文件（tune 唯一会写的状态文件；测试可 monkeypatch 本模块名）
APPLIED_FILE = APPLIED_SCENARIOS_FILE
# MTP>1 应用后自动冒烟（对照方案文档：draft 变更需实测，失败自动回滚）
SMOKE_REQUEST = {
    "model": "__MODEL__",
    "messages": [{"role": "user", "content": "1+1=?"}],
    "max_tokens": 8,
    "temperature": 0,
}


class TuneError(Exception):
    """可读的调优错误（未知预设 / 无引擎 / 无应用层等）。"""


# ── 读取 ─────────────────────────────────────────────────────────

def list_presets(model) -> list[str]:
    """模型可用场景名（按 YAML 顺序）。"""
    return list((model.presets or {}).keys())


def get_active(model) -> str:
    """当前应用的场景名（'' = 未应用；load_models 时由应用层 overlay 填充）。"""
    return model.active_preset or ""


def _engine_cfg(model):
    cfg = model.engine_config
    if cfg is None:
        raise TuneError(f"{model.name}: 没有可调优的引擎配置块（type={model.type}）")
    return cfg


def _adapter(model):
    from .engine_adapter import get_adapter
    try:
        return get_adapter(model.type)
    except KeyError as e:
        raise TuneError(f"{model.name}: 未知引擎 {model.type}") from e


def _fields(model) -> list[str]:
    fields = _adapter(model).scenario_fields(model)
    if not fields:
        raise TuneError(f"{model.name}（{model.type}）暂不支持场景调优（适配器未声明白名单）")
    return fields


def _clamp(values: dict, caps: dict) -> dict:
    """按引擎上限钳制,返回 (钳制值, notes)。"""
    out = {}
    notes = []
    for k, v in values.items():
        cap = caps.get(k) or {}
        lo, hi, step = cap.get("min"), cap.get("max"), cap.get("step")
        orig = v
        v = _coerce(v)
        if lo is not None and v is not None and v < lo:
            v = lo
        if hi is not None and v is not None and v > hi:
            v = hi
        if step and v is not None and v % step:
            v = (v // step) * step  # 向下对齐（如 prefill_chunk 128 倍数）
        if v != orig:
            notes.append(f"{k} {orig} → {v}（引擎上限钳制）")
        out[k] = v
    return out, notes


def _coerce(v):
    """按现值类型转换（YAML 读出的值可能是 str/float）。bool 必须保持。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return v
    return v


# ── 应用层（唯一状态文件，tune 只写它）─────────────────────────

def _load_applied() -> dict:
    if not APPLIED_FILE.exists():
        return {}
    try:
        raw = yaml.safe_load(APPLIED_FILE.read_text()) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception as e:
        log.warning("读取应用层 %s 失败: %s", APPLIED_FILE, e)
        return {}


def _applied_entry(model) -> dict | None:
    return _load_applied().get(model.name)


def _save_applied_entry(model, entry: dict | None):
    """写该模型的应用层条目（entry=None → 删除）。整文件原子重写。"""
    data = _load_applied()
    if entry is None:
        data.pop(model.name, None)
    else:
        data[model.name] = entry
    APPLIED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = APPLIED_FILE.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    os.replace(tmp, APPLIED_FILE)


def _yaml_only_values(model) -> dict:
    """纯模型 YAML 值（不叠加应用层），白名单字段——default 回滚目标。"""
    if not model.yaml_path:
        raise TuneError(f"{model.name}: 无源 YAML 路径（yaml_path 未设置）")
    fresh = load_models(Path(model.yaml_path).parent, include_applied=False)
    src = fresh.get(model.name)
    if src is None or src.engine_config is None:
        raise TuneError(f"{model.name}: 读不到源 YAML 值（{model.yaml_path}）")
    return {f: _coerce(getattr(src.engine_config, f, None)) for f in _fields(model)}


# ── 预览（diff,不落盘）──────────────────────────────────────────

def _target_values(model, preset: str) -> dict:
    """preset 的目标字段值（default = 回退纯 YAML 值）。只取白名单内字段。"""
    if preset == "default":
        if not _applied_entry(model):
            raise TuneError(f"{model.name}: 未应用过场景（无应用层），无需回滚")
        return _yaml_only_values(model)
    pv = (model.presets or {}).get(preset)
    if pv is None:
        raise TuneError(
            f"未知场景 {preset!r}，可选: {', '.join(list_presets(model)) or '(无)'}")
    fields = _fields(model)
    # 只取白名单内字段,其余忽略（坏场景不会污染无关配置）
    return {k: v for k, v in pv.items() if k in fields}


def _current_values(model) -> dict:
    cfg = _engine_cfg(model)
    return {f: _coerce(getattr(cfg, f, None)) for f in _fields(model)}


def preview(model, preset: str) -> dict:
    """计算 before→after diff + 校验 issue,不写盘不重启。

    返回: {model, preset, active_preset, before, after, changed, issues, clamp_notes}
    """
    adapter = _adapter(model)
    target_raw = _target_values(model, preset)
    caps = adapter.engine_caps(model)
    after, clamp_notes = _clamp(target_raw, caps)
    before = _current_values(model)
    issues = list(adapter.validate_scenario(model, after))
    changed = {k: before.get(k) for k in after if before.get(k) != after.get(k)}
    return {
        "model": model.name,
        "type": model.type,
        "preset": preset,
        "active_preset": get_active(model),
        "before": {k: before.get(k) for k in after},
        "after": after,
        "changed": changed,
        "issues": issues,
        "clamp_notes": clamp_notes,
    }


# ── 重启后健康等待 ──────────────────────────────────────────────

def _wait_health(adapter, model, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if adapter.check_health(model) == "✅":
                return True
        except Exception:
            pass
        time.sleep(1)
    return adapter.check_health(model) == "✅"


def _mtp_smoke(model) -> bool:
    """draft>1 场景应用后冒烟: 一条短请求,断言 200。返回是否通过。"""
    import json
    import urllib.request
    try:
        port = _engine_cfg(model).port
        body = json.dumps(SMOKE_REQUEST).replace("__MODEL__", model.name or "default").encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except Exception as e:
        log.warning("[tune] MTP 冒烟失败: %s", e)
        return False


def _restore_previous(model, prev_entry, adapter, proc) -> dict:
    """还原上一次应用层条目 + 内存值，并用旧配置重启。返回重启结果。"""
    _save_applied_entry(model, prev_entry)
    if prev_entry and isinstance(prev_entry.get("overrides"), dict):
        vals = {k: _coerce(v) for k, v in prev_entry["overrides"].items()}
        act = str(prev_entry.get("active_preset") or "")
    else:
        vals = _yaml_only_values(model)
        act = ""
    cfg = _engine_cfg(model)
    for k, v in vals.items():
        setattr(cfg, k, v if not isinstance(v, bool) else bool(v))
    model.active_preset = act
    try:
        adapter.set_process_manager(proc)
    except Exception as e:
        log.warning("[tune] set_process_manager 失败(回滚重启将走基类 stop+start): %s", e)
    return adapter.restart(model)


# ── 应用 ────────────────────────────────────────────────────────

def apply(model, preset: str, dry: bool = False, restart: bool = True, mgr=None) -> dict:
    """应用场景（或 default 回滚）。CLI 与 /admin/tune 共用。

    只写应用层（APPLIED_FILE）+ 内存，模型 YAML 永不改。
    dry=True: 只算 diff。restart=False: 写应用层不重启。
    mgr: ModelManager（重启编排用）；为 None 时跳过重启并提示。
    返回含 status/diff/restart 详情的 dict; TuneError 表示可读失败。
    """
    p = preview(model, preset)
    blocking = [i for i in p["issues"] if i.startswith("❌")]
    if blocking:
        raise TuneError("；".join(blocking))

    if dry:
        return {"status": "preview", **p}

    is_default = preset == "default"
    # 1) 应用层：快照旧条目（失败回滚用），写新条目 / 清空
    prev_entry = _applied_entry(model)
    _save_applied_entry(
        model, None if is_default else
        {"active_preset": preset, "overrides": p["after"]})

    # 2) 内存（本次重启的启动参数来源）
    cfg = _engine_cfg(model)
    for k, v in p["after"].items():
        setattr(cfg, k, v if not isinstance(v, bool) else bool(v))
    model.active_preset = "" if is_default else preset

    # 写盘完成即视为"已应用待重启"（restart=False 或无法重启时停留在该状态）
    result = {"status": "rolled_back_pending" if is_default else "applied_restart_pending",
              "model": model.name, "preset": preset,
              "active_preset": model.active_preset, "diff": p, "restart": None}

    if not restart:
        return result

    if mgr is None:
        result["restart"] = {"status": "skipped", "message": "未提供 mgr，仅写入应用层，重启后生效"}
        return result

    adapter = _adapter(model)
    # 确保适配器持有与 mgr 配套的进程管理器（重启走 GPU 状态机）
    proc = getattr(mgr, "_proc", None) or mgr
    try:
        adapter.set_process_manager(proc)
    except Exception as e:
        log.warning("[tune] set_process_manager 失败(重启将走基类 stop+start): %s", e)
    restarted = adapter.restart(model)
    result["restart"] = restarted

    if restarted.get("status") in ("switched", "ok", "started"):
        result["status"] = "rolled_back" if is_default else "applied"
        # MTP>1 场景: 自动冒烟,失败还原上一次应用层
        if not is_default and p["after"].get("draft_tokens", 0) and p["after"]["draft_tokens"] > 1:
            ok = _mtp_smoke(model)
            result["mtp_smoke"] = ok
            if not ok:
                log.warning("[tune] MTP 冒烟失败 → 还原上一次应用层")
                roll = _restore_previous(model, prev_entry, adapter, proc)
                result["rollback_restart"] = roll
                result["status"] = "rolled_back"
                result["error"] = "MTP 冒烟失败，已回滚"
        return result

    # 启动失败: 还原上一次应用层 + 内存值,重启旧配置
    log.error("[tune] 重启失败 %s → 回滚", restarted.get("message"))
    try:
        roll = _restore_previous(model, prev_entry, adapter, proc)
    except TuneError as e:
        result["status"] = "failed_rollback_error"
        result["error"] = str(e)
        return result
    result["rollback_restart"] = roll
    result["error"] = f"重启失败: {restarted.get('message')}"
    result["status"] = ("failed_rolled_back"
                        if roll.get("status") in ("switched", "ok", "started")
                        else "failed_rollback_failed")
    return result
