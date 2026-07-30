"""Tests for inferfabric.proxy.request_logger — RequestLogger"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from inferfabric.proxy.request_logger import RequestLog, RequestLogger


class TestRequestLog:
    """RequestLog dataclass"""

    def test_defaults(self):
        entry = RequestLog(req_id="iff-1", key_name="primary", model="test", status=200)
        assert entry.route == "local"
        assert entry.cloud_provider is None
        assert entry.ttft_ms is None
        assert entry.tokens_in == 0
        assert entry.error is None

    def test_asdict(self):
        entry = RequestLog(
            req_id="iff-1", key_name="primary", model="qwen36-35b",
            status=200, ttft_ms=487.5, duration_ms=2100, route="local",
        )
        d = entry.__dict__.copy()
        d["ts"] = "2026-07-30T12:00:00+08:00"
        line = json.dumps(d, ensure_ascii=False)
        assert "iff-1" in line
        assert "487.5" in line


class TestRequestLoggerDisabled:
    """enabled=False → 不写日志"""

    def test_disabled_no_file(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=False)
        entry = RequestLog(req_id="iff-1", key_name="primary", model="test", status=200)
        logger.log(entry)
        # No files created
        assert list(tmp_path.iterdir()) == []

    def test_disabled_log_is_noop(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=False)
        assert not logger.enabled
        # Should not raise
        logger.log(RequestLog(req_id="iff-1", key_name="p", model="m", status=200))


class TestRequestLoggerEnabled:
    """enabled=True → 写 JSONL"""

    def test_write_single_entry(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=True)
        entry = RequestLog(
            req_id="iff-7f3a", key_name="primary", model="qwen36-35b",
            status=200, ttft_ms=487, duration_ms=2100, route="local",
        )
        logger.log(entry)
        logger.close()

        today = date.today().isoformat()
        log_file = tmp_path / f"access-{today}.jsonl"
        assert log_file.exists()

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["req_id"] == "iff-7f3a"
        assert data["model"] == "qwen36-35b"
        assert data["ttft_ms"] == 487
        assert data["route"] == "local"

    def test_write_multiple_entries(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=True)
        for i in range(3):
            logger.log(RequestLog(
                req_id=f"iff-{i}", key_name="primary", model="test", status=200,
            ))
        logger.close()

        today = date.today().isoformat()
        lines = (tmp_path / f"access-{today}.jsonl").read_text().strip().split("\n")
        assert len(lines) == 3

    def test_auto_timestamp(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=True)
        entry = RequestLog(req_id="iff-1", key_name="p", model="m", status=200)
        logger.log(entry)
        logger.close()

        today = date.today().isoformat()
        data = json.loads((tmp_path / f"access-{today}.jsonl").read_text().strip())
        assert data["ts"]  # non-empty
        assert "2026" in data["ts"]  # reasonable year

    def test_cloud_route(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=True)
        logger.log(RequestLog(
            req_id="iff-1", key_name="primary", model="deepseek-v4-flash",
            status=200, route="cloud", cloud_provider="baidu-codingplan",
        ))
        logger.close()

        today = date.today().isoformat()
        data = json.loads((tmp_path / f"access-{today}.jsonl").read_text().strip())
        assert data["route"] == "cloud"
        assert data["cloud_provider"] == "baidu-codingplan"

    def test_error_entry(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=True)
        logger.log(RequestLog(
            req_id="iff-1", key_name="primary", model="test",
            status=429, error="rate_limit",
        ))
        logger.close()

        today = date.today().isoformat()
        data = json.loads((tmp_path / f"access-{today}.jsonl").read_text().strip())
        assert data["status"] == 429
        assert data["error"] == "rate_limit"


class TestRequestLoggerRotation:
    """按日轮转"""

    def test_close_and_reopen(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=True)
        logger.log(RequestLog(req_id="iff-1", key_name="p", model="m", status=200))
        logger.close()
        assert logger._fd is None

        # Re-log should reopen
        logger.log(RequestLog(req_id="iff-2", key_name="p", model="m", status=200))
        logger.close()

        today = date.today().isoformat()
        lines = (tmp_path / f"access-{today}.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2
