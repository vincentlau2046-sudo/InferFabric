# Ubiquitous Language — IFF v4.6.5

| 术语 | 含义 |
|------|------|
| MetricsAggregator | 内存滑动窗口聚合器，从 queue 消费 RequestLog |
| RequestLogDB | SQLite 持久化存储，WAL 模式 |
| DualGateLimiter | 二级门限流器（RPM + 并发 Semaphore） |
| Observe mode | 流控观察模式：超限只记录不拒绝 |
| served_name | vLLM 注册的模型标识符，如 `vllm_qwen27b_vl` |
| friendly_name | IFF 模型配置的友好名，如 `qwen36-27b-vl` |
| SSELineBuffer | SSE 流式行缓冲 + usage 提取器 |
| 空窗口 | 指定时间范围内无请求的 metrics 查询 |
