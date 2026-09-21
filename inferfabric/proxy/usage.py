"""usage 字段归一化 — 双协议 (OpenAI / Anthropic) usage → 统一统计口径。

统一语义：
- prompt_tokens        = 总输入（完整上下文，含缓存命中部分）
- prompt_tokens_cached = 缓存命中部分
- completion_tokens    = 输出

源协议差异：
- OpenAI 系（vLLM/SGLang/OpenAI）：prompt_tokens 本身即总输入（已含缓存部分），
  缓存子集在 prompt_tokens_details.cached_tokens，不能重复相加。
- Anthropic 系（NInfer Anthropic 协议 / 云端 Anthropic）：input_tokens 仅是
  未缓存的新增输入，命中缓存在 cache_read_input_tokens，新写入缓存的输入在
  cache_creation_input_tokens — 三者相加才是总 prompt。
"""

ZERO = {"prompt_tokens": 0, "prompt_tokens_cached": 0, "completion_tokens": 0}


def normalize_usage(usage) -> dict:
    """归一化单个 usage dict → {prompt_tokens, prompt_tokens_cached, completion_tokens}。

    纯函数，只读不 mutate 输入。非 dict 输入返回全零。
    兼容百度风格（input/output 命名、无 cache 字段）：按 Anthropic 语义处理，
    cache 字段缺失即为 0，结果与 OpenAI 无缓存等价。
    """
    if not isinstance(usage, dict):
        return dict(ZERO)
    pt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    ct = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    cached = 0
    if "prompt_tokens" in usage:
        # OpenAI 系：prompt_tokens 已含缓存部分
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = details.get("cached_tokens") or 0
    else:
        # Anthropic 系：input_tokens 不含缓存，补回 cache_read（命中）与
        # cache_creation（新写入，计入总量但不算命中）
        cached = usage.get("cache_read_input_tokens") or 0
        pt = pt + cached + (usage.get("cache_creation_input_tokens") or 0)
    return {
        "prompt_tokens": int(pt),
        "prompt_tokens_cached": int(cached),
        "completion_tokens": int(ct),
    }
