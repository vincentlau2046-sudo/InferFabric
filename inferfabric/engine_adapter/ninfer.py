"""NInferAdapter — NInfer Docker inference engine.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from inferfabric.engine_adapter.base import EngineAdapter
from inferfabric.engine_adapter import register

if TYPE_CHECKING:
    from inferfabric.config import ModelConfig

log = logging.getLogger("inferfabric.ninfer_adapter")


class NInferAdapter(EngineAdapter):
    def __init__(self, process_manager=None):
        self._proc = process_manager

    @property
    def engine_type(self) -> str:
        return "ninfer"

    def check_health(self, model: ModelConfig) -> str:
        try:
            from inferfabric.health import check_http_status
            port = model.ninfer.port if model.ninfer else None
            if port:
                return check_http_status(f"http://localhost:{port}/v1/models")
            return "?"
        except Exception as e:
            log.warning("[ninfer] health check failed for %s: %s", model.name, e)
            return "?"

    def get_context_window(self, model: ModelConfig) -> int | None:
        return model.ninfer.max_context if model.ninfer else None

    def get_metadata(self, model: ModelConfig) -> dict:
        if not model.ninfer:
            return {}
        return {
            "context_window": self.get_context_window(model),
            "weight_path": model.ninfer.weight_path,
            "docker_image": model.ninfer.docker_image,
        }

    def validate_config(self, model: ModelConfig) -> list[str]:
        issues = []
        if not model.ninfer:
            return ["Missing ninfer config block"]
        cfg = model.ninfer
        if not cfg.weight_path:
            issues.append("ninfer.weight_path is empty")
        if not cfg.docker_image:
            issues.append("ninfer.docker_image is empty")
        if cfg.port <= 0:
            issues.append(f"Invalid ninfer.port: {cfg.port}")
        weight = Path(cfg.weight_path).expanduser()
        if not weight.exists():
            issues.append(f"Weight file not found: {weight}")
        return issues

    def start(self, model: ModelConfig) -> dict:
        """Start NInfer via ProcessManager delegation (D1).

        container_name threaded from ModelConfig.container_name (single source).
        """
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = model.ninfer
        if not cfg:
            return {"status": "error", "message": "No ninfer config"}
        return self._proc.start_ninfer(cfg, model.container_name)

    def stop(self, model: ModelConfig) -> dict:
        """Stop NInfer via ProcessManager delegation (migrated off base helper, D1)."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = model.ninfer
        if not cfg:
            return {"status": "error", "message": "No ninfer config"}
        return self._proc.stop_ninfer(model.container_name)

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "✅"

    def get_port(self, model: ModelConfig) -> int | None:
        return model.ninfer.port if model.ninfer else None

    def get_pid_state_key(self) -> str | None:
        return 'ninfer_pid'

    
    def fetch_engine_metrics(self, model: ModelConfig) -> dict | None:
        if not model.ninfer: return None
        import re as _re
        from pathlib import Path
        cfg = model.ninfer
        log_path = Path(cfg.log_file or f"/tmp/ninfer-{cfg.port}.log")
        if not log_path.exists(): return None
        size = log_path.stat().st_size
        chunk = min(size, 500 * 1024)
        with open(log_path) as f:
            if chunk < size: f.seek(size - chunk); f.readline()
            lines = f.readlines()
        cap = None; reqs = []; tput = None; running = None
        for line in lines:
            line = line.strip()
            # live running batch：throughput 行每 5s 打印 `running N`，取最近一条。
            # 覆盖 prefill-only / decode / idle（running 0）所有行，非累计。
            if "throughput" in line:
                mr = _re.search(r"running\s+(\d+)", line)
                if mr: running = int(mr.group(1))
            m = _re.search(r"capacity.*?pages\s+([\d,]+)/([\d,]+)", line)
            if m: cap = (int(m.group(1).replace(",","")), int(m.group(2).replace(",",""))); continue
            m = _re.search(r"throughput.*?decode\s+([\d.]+)(k?)\s+tok", line)
            if m: tput = float(m.group(1)) * 1000 if m.group(2)=="k" else float(m.group(1)); continue
            m = _re.search(r"req#\d+\s+done.+?prompt\s+([\d,]+)\s*\|.+?output\s+([\d,]+).+?TTFT\s+([\d.]+)\s*(ms|s).+?decode\s+([\d.]+)k?\s*", line)
            if m:
                p = int(m.group(1).replace(",",""))
                o = int(m.group(2).replace(",",""))
                t = float(m.group(3))/1000 if m.group(4)=="ms" else float(m.group(3))
                ds = m.group(5); dl = m.group(0)
                av = dl[dl.find(ds)+len(ds):]
                dt = float(ds)*1000 if av.lstrip().startswith("k") else float(ds)
                reqs.append({"prompt":p,"output":o,"ttft_s":t,"tpot_s":1.0/dt if dt>0 else 0})
        r = {"sleep_state": 0}
        if cap: r["kv_cache_usage_perc"] = round(cap[0]/cap[1]*100, 1)
        rec = reqs[-50:] if len(reqs)>50 else reqs
        if rec:
            r["seq_length"] = int(sum(x["prompt"]+x["output"] for x in rec)/len(rec))
            r["seq_prompt"] = int(sum(x["prompt"] for x in rec)/len(rec))
            r["seq_generation"] = int(sum(x["output"] for x in rec)/len(rec))
            r["seq_count"] = len(rec)
            ts = sorted(x["ttft_s"] for x in rec)
            r["ttft_cum_mean"] = round(sum(ts)/len(ts),3); r["ttft_cum_n"] = len(ts)
            r["ttft_seconds"] = {"mean":round(sum(ts)/len(ts),3),"p50":round(ts[len(ts)//2],3),"p95":round(ts[int(len(ts)*0.95)],3),"count":len(ts)}
            ps = sorted(x["tpot_s"] for x in rec)
            r["tpot_cum_mean"] = round(sum(ps)/len(ps),4); r["tpot_cum_n"] = len(ps)
            r["tpot_seconds"] = {"p50":round(ps[len(ps)//2],4),"p95":round(ps[int(len(ps)*0.95)],4),"mean":round(sum(ps)/len(ps),4),"count":len(ps)}
        if tput: r["throughput"] = str(round(tput,1)); r["throughput_inst"] = str(round(tput,1))
        if rec: r["throughput_cum_n"] = sum(x["output"] for x in rec)
        # Batch Size：当前在途并发请求数（live running batch，0..max_concurrency），
        # 非 seq_count 累计完成数。max_batch 来自引擎配置上限。
        if running is not None:
            r["running_batch"] = running
        r["max_batch"] = cfg.max_concurrency
        return r if r.get("kv_cache_usage_perc") or rec or running is not None else {"sleep_state": 0}


register("ninfer", NInferAdapter)