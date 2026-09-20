"""NInferAdapter — NInfer Docker inference engine.
"""
from __future__ import annotations
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING

from inferfabric.engine_adapter.base import EngineAdapter
from inferfabric.engine_adapter import register

if TYPE_CHECKING:
    from inferfabric.config import ModelConfig

log = logging.getLogger("inferfabric.ninfer_adapter")


class NInferAdapter(EngineAdapter):

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
        import logging
        import subprocess
        import time
        log = logging.getLogger("inferfabric")
        cfg = model.ninfer
        if not cfg:
            return {"status": "error", "message": "No ninfer config"}

        container = cfg.container_name or f"ninfer-{cfg.port}"
        subprocess.run(["docker", "stop", container], timeout=10,
                       capture_output=True, check=False)
        time.sleep(2)

        weight_path = Path(cfg.weight_path).expanduser()
        weight_dir = str(weight_path.parent)

        cmd = [
            "docker", "run", "--gpus", "all", "--rm",
            "-v", f"{weight_dir}:/workspace",
            "-p", f"{cfg.port}:8080",
            "--name", container,
            "-e", "NVIDIA_DISABLE_REQUIRE=1",
            cfg.docker_image,
            "ninfer-serve", weight_path.name,
            "--model-id", cfg.model_id,
            "--host", "0.0.0.0", "--port", "8080",
            "--max-concurrency", str(cfg.max_concurrency),
            "--max-context", str(cfg.max_context),
            "--kv-capacity", "auto" if cfg.kv_capacity == 0 else str(cfg.kv_capacity),
            "--default-max-tokens", str(cfg.default_max_tokens),
            "--pending-timeout-ms", str(cfg.pending_timeout_ms),
            "--kv-dtype", cfg.kv_dtype,
            "--prefill-chunk", str(cfg.prefill_chunk),
        ]
        if cfg.enable_mtp:
            cmd.extend(["--spec", "mtp", "--draft-tokens", str(cfg.draft_tokens)])
        if cfg.enable_lm_head_draft:
            cmd.append("--lm-head-draft")

        log.info("Starting NInfer: %s", " ".join(cmd))
        log_file = Path(cfg.log_file or f"/tmp/ninfer-{cfg.port}.log")
        log_file.write_text("")

        try:
            proc = subprocess.Popen(
                cmd, stdout=log_file.open("a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:
            log.error("Failed to start NInfer: %s", e)
            return {"status": "error", "message": f"Popen failed: {e}"}

        from inferfabric.health import wait_http
        timeout = cfg.startup_timeout or 120
        healthy = wait_http(f"http://localhost:{cfg.port}/v1/models",
                           timeout=timeout)
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        return {"status": "timeout",
                "message": f"NInfer didn't become healthy within {timeout}s"}

    def stop(self, model: ModelConfig) -> dict:
        import logging
        import subprocess
        log = logging.getLogger("inferfabric")
        cfg = model.ninfer
        if not cfg:
            return {"status": "error", "message": "No ninfer config"}
        container = model.container_name
        log.info("Stopping NInfer container: %s", container)
        result = subprocess.run(
            ["docker", "stop", container],
            timeout=30, capture_output=True, check=False)
        if result.returncode == 0:
            return {"status": "ok", "message": f"Container {container} stopped"}
        msg = result.stderr.decode()[:200]
        return {"status": "warning",
                "message": f"docker stop exit {result.returncode}: {msg}"}

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "✅"

    def get_port(self, model: ModelConfig) -> int | None:
        return model.ninfer.port if model.ninfer else None

    
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
        cap = None; reqs = []; tput = None
        for line in lines:
            line = line.strip()
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
        return r if r.get("kv_cache_usage_perc") or rec else {"sleep_state": 0}


def get_pid_state_key(self) -> str | None:
        return None


register("ninfer", NInferAdapter)