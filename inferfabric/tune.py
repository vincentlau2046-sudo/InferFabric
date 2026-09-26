"""iff tune — 场景预设调优编排器（引擎无关）。

场景定义单一事实源：侧车 `models.d/scenarios.yaml`（顶层键=模型名，手动配置、
可 git 审），同名场景覆盖模型 YAML 内联 `presets:`（内联仍支持）。
`active_preset:` 记录当前应用场景（写入模型 YAML）。
本模块只依赖 ModelConfig + EngineAdapter 的可选钩子（scenario_fields / engine_caps /
validate_scenario / restart），**不 import 任何具体引擎**——未来 vLLM/SGLang 实现
适配器钩子即可接入，CLI 与 /admin/tune 流程零改动。

流程：读 YAML presets → 引擎白名单过滤 → 按引擎上限钳制 → KV 池顶校验 →
before→after diff → 写回引擎 config 块 + YAML + active_preset → 重启（可 --dry）。
首次应用前快照基线（~/.inferfabric/tune_baselines.yaml），`default` 一键回滚。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .engine_adapter import get_adapter

if TYPE_CHECKING:
    from .config import ModelConfig

log = logging.getLogger("inferfabric.tune")

IFF_DATA_DIR = Path.home() / ".inferfabric"
BASELINE_FILE = IFF_DATA_DIR / "tune_baselines.yaml"
# 应用前对源 YAML 的字节级备份（防写坏 / 防启动失败回滚），保留原注释
_BACKUP_SUFFIX = ".tune-bak"

# MTP>1 应用后自动冒烟（对照方案文档：draft 变更需实测，失败自动回滚）
SMOKE_REQUEST = {
    "model": "__MODEL__",
    "messages": [{"role": "user", "content": "1+1=?"}],
    "max_tokens": 8,
    "temperature": 0,
}


class TuneError(Exception):
    """可读的调优错误（未知预设 / 无引擎 / 无基线等）。"""


# ── 读取 ─────────────────────────────────────────────────────────

def list_presets(model: ModelConfig) -> list[str]:
    """模型可用场景名（按 YAML 顺序）。"""
    return list((model.presets or {}).keys())


def get_active(model: ModelConfig) -> str:
    """当前应用的场景名（'' = 未应用）。"""
    return model.active_preset or ""


def _engine_cfg(model: ModelConfig):
    cfg = model.engine_config
    if cfg is None:
        raise TuneError(f"{model.name}: 没有可调优的引擎配置块（type={model.type}）")
    return cfg


def _adapter(model: ModelConfig):
    try:
        return get_adapter(model.type)
    except KeyError as e:
        raise TuneError(f"{model.name}: 未知引擎 {model.type}") from e


def _fields(model: ModelConfig) -> list[str]:
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


# ── 基线（首次应用快照，供 default 回滚）────────────────────────

def _load_baselines() -> dict:
    try:
        if BASELINE_FILE.exists():
            raw = yaml.safe_load(BASELINE_FILE.read_text()) or {}
            return raw if isinstance(raw, dict) else {}
    except Exception as e:
        log.warning("读取基线 %s 失败: %s", BASELINE_FILE, e)
    return {}


def _save_baseline_once(model: ModelConfig):
    """首次应用时快照原始引擎字段（当前 live 值，未应用任何预设）。"""
    bl = _load_baselines()
    if model.name in bl:
        return  # 已有基线,不覆盖（保持"应用前"的原点）
    cfg = _engine_cfg(model)
    fields = _fields(model)
    bl[model.name] = {f: _coerce(getattr(cfg, f, None)) for f in fields}
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BASELINE_FILE.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(bl, allow_unicode=True, sort_keys=False))
    os.replace(tmp, BASELINE_FILE)


def _baseline(model: ModelConfig) -> dict:
    bl = _load_baselines()
    return bl.get(model.name) or {}


# ── 预览（diff,不落盘）──────────────────────────────────────────

def _target_values(model: ModelConfig, preset: str) -> dict:
    """preset 的目标字段值（default = 基线回滚）。只取白名单内字段。"""
    if preset == "default":
        base = _baseline(model)
        if not base:
            raise TuneError(
                f"{model.name}: 没有已应用过预设（基线不存在），无需回滚")
        return dict(base)
    pv = (model.presets or {}).get(preset)
    if pv is None:
        raise TuneError(
            f"未知场景 {preset!r}，可选: {', '.join(list_presets(model)) or '(无)'}")
    fields = _fields(model)
    # 只取白名单内字段,其余忽略（坏预设不会污染无关配置）
    return {k: v for k, v in pv.items() if k in fields}


def _current_values(model: ModelConfig) -> dict:
    cfg = _engine_cfg(model)
    return {f: _coerce(getattr(cfg, f, None)) for f in _fields(model)}


def preview(model: ModelConfig, preset: str) -> dict:
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


# ── 写回 YAML（保留注释的字节级来源已备份到 .tune-bak）──────────

def _yaml_path(model: ModelConfig) -> Path:
    return Path(model.yaml_path) if model.yaml_path else Path()


def _write_yaml(model: ModelConfig, after: dict, preset: str):
    """把目标字段写进引擎 config 块 + 顶层 active_preset（原子替换）。

    注释在首次应用后不保留（备份在 .tune-bak 与 git 历史）。
    """
    path = _yaml_path(model)
    if not path:
        raise TuneError(f"{model.name}: 无源 YAML 路径，无法写回（yaml_path 未设置）")
    if not path.exists():
        raise TuneError(f"{model.name}: 源 YAML 不存在: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    block = raw.setdefault(model.type, {})
    for k, v in after.items():
        block[k] = v if not isinstance(v, bool) else bool(v)
    if preset == "default":
        raw.pop("active_preset", None)
    else:
        raw["active_preset"] = preset
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))
    os.replace(tmp, path)


def _backup_yaml(model: ModelConfig) -> Path | None:
    path = _yaml_path(model)
    if not path or not path.exists():
        return None
    bak = path.with_suffix(path.suffix + _BACKUP_SUFFIX)  # 例: Qwen38-27B-TXT.yaml.tune-bak
    bak.write_bytes(path.read_bytes())
    return bak


def _restore_yaml(model: ModelConfig, bak: Path | None):
    if bak is None:
        return
    path = _yaml_path(model)
    if path.exists():
        path.write_bytes(bak.read_bytes())


def _reload_values_from_yaml(model: ModelConfig) -> dict:
    """启动失败回滚用：从备份 YAML 读回原始字段值并写回内存 cfg。"""
    from .config import load_models
    fresh = load_models(Path(model.yaml_path).parent)
    if model.name not in fresh:
        return {}
    src = fresh[model.name]
    src_cfg = src.engine_config
    if src_cfg is None:
        return {}
    cfg = _engine_cfg(model)
    for f in _fields(model):
        setattr(cfg, f, _coerce(getattr(src_cfg, f, None)))
    model.active_preset = getattr(src, "active_preset", "") or ""
    return {f: getattr(cfg, f) for f in _fields(model)}


# ── 重启后健康等待 ──────────────────────────────────────────────

def _wait_health(adapter, model: ModelConfig, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if adapter.check_health(model) == "✅":
                return True
        except Exception:
            pass
        time.sleep(1)
    return adapter.check_health(model) == "✅"


def _mtp_smoke(model: ModelConfig) -> bool:
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


# ── 应用 ────────────────────────────────────────────────────────

def apply(model: ModelConfig, preset: str, dry: bool = False,
          restart: bool = True, mgr=None) -> dict:
    """应用场景（或 default 回滚）。CLI 与 /admin/tune 共用。

    dry=True: 只算 diff 不写盘不重启。restart=False: 只写配置不重启。
    mgr: ModelManager（重启编排用）；为 None 时若需重启则跳过重启并提示。
    返回含 status/diff/restart 详情的 dict; TuneError 表示可读失败。
    """
    p = preview(model, preset)
    blocking = [i for i in p["issues"] if i.startswith("❌")]
    if blocking:
        raise TuneError("；".join(blocking))

    if dry:
        return {"status": "preview", **p}

    # 1) 基线 + 备份 + 写回
    _save_baseline_once(model)
    bak = _backup_yaml(model)
    after = p["after"]
    cfg = _engine_cfg(model)
    for k, v in after.items():
        setattr(cfg, k, v if not isinstance(v, bool) else bool(v))
    _write_yaml(model, after, preset)
    active = "" if preset == "default" else preset
    model.active_preset = active
    # 写盘完成即视为"已应用待重启"（restart=False 或无法重启时停留在该状态）
    result = {"status": "applied_restart_pending", "model": model.name, "preset": preset,
              "active_preset": active, "diff": p, "restart": None}

    # 2) 重启
    if restart:
        if mgr is None:
            path = _yaml_path(model)
            result["restart"] = {"status": "skipped", "message": "未提供 mgr，仅写入配置，重启后生效"}
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
            result["status"] = "applied"
            # MTP>1 场景: 自动冒烟,失败回滚基线
            if after.get("draft_tokens", 0) and after["draft_tokens"] > 1:
                ok = _mtp_smoke(model)
                result["mtp_smoke"] = ok
                if not ok:
                    log.warning("[tune] MTP 冒烟失败 → 回滚到基线")
                    try:
                        roll = apply(model, "default", dry=False, restart=True, mgr=mgr)
                        result["rollback"] = roll
                        result["status"] = "rolled_back"
                    except TuneError as e:
                        result["status"] = "mtp_failed_rollback_error"
                        result["error"] = str(e)
        else:
            # 启动失败: 还原 YAML + 重启旧配置
            log.error("[tune] 重启失败 %s → 回滚", restarted.get("message"))
            _restore_yaml(model, bak)
            prev = _reload_values_from_yaml(model)
            try:
                adapter.set_process_manager(proc)
                roll_bak = _backup_yaml(model)
                roll = adapter.restart(model)
                result["rollback_restart"] = {**roll, "restored_values": prev}
                result["status"] = "failed_rolled_back"
                result["error"] = f"重启失败: {restarted.get('message')}"
                if roll.get("status") in ("switched", "ok", "started"):
                    result["status"] = "failed_rolled_back"
                else:
                    result["status"] = "failed_rollback_failed"
            except TuneError as e:
                result["status"] = "failed_rollback_error"
                result["error"] = str(e)
    return result