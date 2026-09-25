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
        reqs = []; tput = None; run_samples = []; saw_running = False
        for line in lines:
            line = line.strip()
            # live running batch：throughput 行每 5s 打印 `running N`（在途并发数，非累计）。
            # 收非零采样供 running_batch 求平均（跳过 idle 间隙的 0，避免单点跌 0）；
            # saw_running 标记引擎存活（即便全 0 idle 也算活着）。
            if "throughput" in line:
                mr = _re.search(r"running\s+(\d+)", line)
                if mr:
                    v = int(mr.group(1))
                    saw_running = True
                    if v > 0:
                        run_samples.append(v)
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
        # 日志派生兜底值（引擎 /metrics 不可用时的旧行为）。
        if tput: r["throughput"] = str(round(tput,1)); r["throughput_inst"] = str(round(tput,1))
        if rec: r["throughput_cum_n"] = sum(x["output"] for x in rec)
        # 日志 running_batch 兜底：取最近 20 条非零采样的平均（引擎 /metrics 不可用时保留）。
        if run_samples:
            r["running_batch"] = round(sum(run_samples[-20:]) / len(run_samples[-20:]), 1)
        # 权威引擎值：NInfer 引擎 GET /metrics（Prometheus 文本）经共享 VllmMetricsCollector
        # （prefix="ninfer_"）取 KV / running batch / 吞吐（EMA）+ 直方图派生的 seq/TTFT/TPOT。
        # 有值即覆盖上面的日志兜底；旧镜像（无直方图）或 /metrics 不可达时静默回退日志派生值。
        try:
            from urllib.request import urlopen
            from inferfabric.prometheus import VllmMetricsCollector, parse_prometheus_text
            with urlopen(f"http://127.0.0.1:{cfg.port}/metrics", timeout=10) as resp:
                text = resp.read().decode("utf-8")
            gauges, counters, histos = parse_prometheus_text(text)
            engine_r = VllmMetricsCollector.compute(cfg.port, gauges, counters, histos, prefix="ninfer_")
            for k in ("kv_cache_usage_perc", "running_batch", "throughput", "throughput_inst",
                      "throughput_cum_n", "ttft_seconds", "ttft_cum_mean", "ttft_cum_n",
                      "tpot_seconds", "tpot_cum_mean", "tpot_cum_n", "seq_length", "seq_prompt",
                      "seq_generation", "seq_count", "prompt_tokens_sum", "generation_tokens_sum"):
                if engine_r.get(k) is not None:
                    r[k] = engine_r[k]
        except Exception:
            pass  # 旧镜像 / 无 /metrics → 保持日志派生值
        r["max_batch"] = cfg.max_concurrency
        has_value = (r.get("kv_cache_usage_perc") is not None or bool(rec) or saw_running
                    or tput is not None)
        return r if has_value else {"sleep_state": 0}


register("ninfer", NInferAdapter)