"""normalize_usage — 双协议 usage 归一化单元测试。

覆盖：
  - OpenAI 基础 / 带 prompt_tokens_details.cached_tokens
  - Anthropic 全量（input + cache_read + cache_creation）
  - Anthropic 无缓存字段（百度风格 input/output 命名）
  - 空 / 非 dict / 双命名冲突（OpenAI 优先）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from inferfabric.proxy.usage import normalize_usage


class TestNormalizeUsageOpenAI:
    def test_basic(self):
        u = {"prompt_tokens": 1000, "completion_tokens": 10}
        r = normalize_usage(u)
        assert r["prompt_tokens"] == 1000
        assert r["prompt_tokens_cached"] == 0
        assert r["completion_tokens"] == 10

    def test_with_cached_details(self):
        """OpenAI 语义：prompt_tokens 已含缓存部分，cached 是子集不重复加。"""
        u = {"prompt_tokens": 1000, "completion_tokens": 10,
             "prompt_tokens_details": {"cached_tokens": 800}}
        r = normalize_usage(u)
        assert r["prompt_tokens"] == 1000
        assert r["prompt_tokens_cached"] == 800

    def test_details_not_dict_guarded(self):
        u = {"prompt_tokens": 5, "completion_tokens": 1,
             "prompt_tokens_details": "weird"}
        r = normalize_usage(u)
        assert r["prompt_tokens"] == 5
        assert r["prompt_tokens_cached"] == 0


class TestNormalizeUsageAnthropic:
    def test_full_with_cache_read(self):
        """真实 ninfer 场景：prompt 145,514 中 129,428 命中缓存。"""
        u = {"input_tokens": 16086, "output_tokens": 2112,
             "cache_read_input_tokens": 129428}
        r = normalize_usage(u)
        assert r["prompt_tokens"] == 145514          # 16086 + 129428
        assert r["prompt_tokens_cached"] == 129428
        assert r["completion_tokens"] == 2112

    def test_cache_creation_counts_in_total_not_cached(self):
        """cache_creation 是新增输入（计入总量）但不是缓存命中。"""
        u = {"input_tokens": 100, "output_tokens": 5,
             "cache_read_input_tokens": 50,
             "cache_creation_input_tokens": 30}
        r = normalize_usage(u)
        assert r["prompt_tokens"] == 180             # 100 + 50 + 30
        assert r["prompt_tokens_cached"] == 50       # 仅 cache_read

    def test_no_cache_fields(self):
        """无缓存字段（首次请求 / 百度风格 input/output 命名）。"""
        u = {"input_tokens": 50, "output_tokens": 5}
        r = normalize_usage(u)
        assert r["prompt_tokens"] == 50
        assert r["prompt_tokens_cached"] == 0
        assert r["completion_tokens"] == 5

    def test_zero_cache_fields_ignored(self):
        u = {"input_tokens": 48447, "output_tokens": 1200,
             "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
        r = normalize_usage(u)
        assert r["prompt_tokens"] == 48447
        assert r["prompt_tokens_cached"] == 0


class TestNormalizeUsageEdgeCases:
    def test_empty_dict(self):
        r = normalize_usage({})
        assert r == {"prompt_tokens": 0, "prompt_tokens_cached": 0,
                     "completion_tokens": 0}

    def test_non_dict_input(self):
        assert normalize_usage(None) == normalize_usage({})
        assert normalize_usage("garbage") == normalize_usage({})

    def test_openai_naming_wins_on_conflict(self):
        """双命名冲突时按 OpenAI 语义（prompt_tokens 含缓存，不再加 cache 字段）。"""
        u = {"prompt_tokens": 100, "input_tokens": 90,
             "completion_tokens": 1, "output_tokens": 1,
             "cache_read_input_tokens": 77}
        r = normalize_usage(u)
        assert r["prompt_tokens"] == 100
        assert r["prompt_tokens_cached"] == 0

    def test_input_not_mutated(self):
        u = {"input_tokens": 10, "cache_read_input_tokens": 5}
        snapshot = dict(u)
        normalize_usage(u)
        assert u == snapshot
