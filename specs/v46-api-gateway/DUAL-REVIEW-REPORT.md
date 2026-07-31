# IFF v4.6.0 Dual-Agent Code Review — Cross-Reference Report

**Date**: 2026-07-30
**Reviewers**: AtomCode (AtomGit-deepseek-v4-flash), OpenCode (baidu-codingplan/deepseek-v4-flash)

## Consensus: 3 CRITICAL Blockers

Both reviewers independently identified the same 3 critical issues:

| # | Issue | AtomCode | OpenCode | Fix |
|---|-------|----------|----------|-----|
| C1 | API key in `cloud_provider.yaml` (git-tracked) | ❌ FAIL | ❌ FAIL | Replace with `${BAIDU_API_KEY}` env var, add to `.gitignore` |
| C4 | Missing CORS headers in `pipe_stream_response` | ❌ FAIL | ⚠️ PASS_WITH_NOTES | Add `Access-Control-Allow-Origin: *` to streaming response |
| C6 | Shell injection via `eval` in `scripts/iff-cloud` | ❌ FAIL | ❌ FAIL | Replace `eval "$cmd"` with bash array |

## Important Issues (Both Agree)

| # | Issue | AC | OC | Priority |
|---|-------|----|----|----------|
| C2 | `cloud_discovery` dict swap not thread-safe | HIGH | HIGH | Add `threading.RLock` |
| I5/I6 | Auth timezone-naive expiry crash | MEDIUM | — | Guard against naive datetime |
| I13 | Cloud routes not logged via RequestLogger | MEDIUM | HIGH | Add logging calls |
| I21 | Cloud model re-lookup may pick wrong provider | MEDIUM | — | Use `provider/model_id` key |
| I1 | Lazy discovery blocks first request (up to 180s) | MEDIUM | MEDIUM | Background thread or cache |

## Unique Findings (One Reviewer Only)

### AtomCode only:
- **I3**: `reload()` stops polling but doesn't restart it
- **I7**: Malformed api_keys.yaml silently disables auth (fail-open)
- **I10**: `access_log` config toggle documented but never wired
- **I22**: Anthropic path has cloud fallback, OpenAI path doesn't

### OpenCode only:
- **SSRF via `include_pattern`** (ReDoS risk)
- **`/admin/cloud/test` SSRF** — no URL scheme validation
- **Cloud provider additions not persisted** to YAML on disk
- **`forward_to_cloud` mutates input `data` dict** (model rewrite)
- **Dashboard admin token in URL query parameter**
- **Log rotation missing** (JSONL files grow indefinitely)
- **`handle_ollama_native` potential connection leak**

## Verdict Comparison

| | AtomCode | OpenCode |
|---|----------|----------|
| Overall | ❌ FAIL | ⚠️ PASS_WITH_NOTES |
| Difference | Stricter on CORS (marks CRITICAL) | CORS marked as Important, not Critical |
| Agreement | Same 3 blockers, same top priorities | Same 3 blockers, same top priorities |

**Reconciled Verdict**: ❌ FAIL — 3 critical blockers must be fixed before production merge.

## Action Plan (Priority Order)

### P0 — Must Fix Before Merge (Blockers)
1. Remove API key from `cloud_provider.yaml`, use env var `${BAIDU_API_KEY}`, add `.gitignore`
2. Add CORS headers to `pipe_stream_response` in `forwarder.py`
3. Rewrite `_curl()` in `scripts/iff-cloud` — replace `eval` with bash array

### P1 — Should Fix Before Merge
4. Add `threading.RLock` to `CloudDiscovery` for `cloud_models` dict swap
5. Guard against timezone-naive datetime in `auth.py` `_KeyEntry.expires`
6. Add `RequestLogger.log()` calls to cloud route paths in `chat_handlers.py`
7. Fix cloud model re-lookup to use `provider/model_id` key after `resolve_route`

### P2 — Can Fix Post-Merge (v4.6.1)
8. Background cloud discovery on startup (non-blocking)
9. Log rotation for request_logger
10. Persist cloud provider additions to YAML
11. `/admin/cloud/test` URL validation (SSRF prevention)
12. Cloud request data mutation fix (deepcopy or restore)
13. Dashboard token in URL → use header or session storage
