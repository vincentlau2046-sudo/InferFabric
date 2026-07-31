# v4.6.1 双 Agent 审查汇总

> 审查时间: 2026-07-31 | AtomCode (GLM-5.2) + Codex (GLM-5.1)

## Critical 汇总（阻塞合入）

| ID | 来源 | 问题 | 文件 | 修复难度 |
|----|------|------|------|---------|
| C1 | AC-R1 + CX-R1 | DualGateLimiter: Gate1(RPM)成功→Gate2(Semaphore)失败时不归还RPM令牌，永久泄漏 | ratelimit.py:210-220 | 中 |
| C2 | AC-R1 + CX-R1 | handler.py cloud路径：先记status=0占位日志再调forward_to_cloud，双写+streaming usage全丢 | handler.py:355-364 | 中 |
| C3 | AC-R1 | acquire/release model_name可能不一致，RPM model bucket归还错配 | chat_handlers.py:245, handler.py:413 | 中 |
| C4 | AC-R2 | AggregatorThread while True无退出机制，daemon线程queue中entry可能丢失 | metrics_aggregator.py:156 | 低 |
| C5 | AC-R2 | _total_*计数器与samples数组trim后不同步，死代码 | metrics_aggregator.py:71 | 低 |
| C6 | AC-R2 | _samples截断操作在锁内大列表拷贝，阻塞record/get_metrics | metrics_aggregator.py:77 | 中 |
| C7 | CX-R3 | save_config()缺flush+fsync，os.replace()前tmp可能未落盘 | cloud_discovery.py:237 | 低 |
| C8 | CX-R3 | _save_lock与_models_lock锁序不一致，reload() vs add/delete | cloud_discovery.py + handler.py | 中 |
| C9 | CX-R3 | _providers在_models_lock外被修改，save_config()无锁读取_providers | handler.py:820-830 | 低 |

## Medium 汇总（建议同批修复）

| ID | 来源 | 问题 | 文件 |
|----|------|------|------|
| M1 | AC-R1 | DualGateLimiter.release(model)的model参数从未使用，RPM一次性消费不归还 | ratelimit.py:222 |
| M2 | AC-R1 | forward_to_cloud用同步urllib阻塞，streaming分支BrokenPipe外异常会破坏HTTP状态机 | forwarder.py:193 |
| M3 | AC-R1 | cloud错误日志可能泄漏上游错误体中的敏感字段 | forwarder.py:218,221 |
| M4 | AC-R1 | _load_price_config在cloud未discover时调用，读到空cloud_models | proxy_manager.py:51-72 |
| M5 | AC-R2 | get_metrics()读取_price_config无锁保护 | metrics_aggregator.py:115 |
| M6 | AC-R2 | Queue()无maxsize，内存无上限 | proxy_manager.py:60 |
| M7 | AC-R2 | request_logger.py put_nowait放原始entry对象（引用传递） | request_logger.py:84 |
| M8 | CX-R3 | _config_corrupt置True后永不重置 | cloud_discovery.py:377 |
| M9 | CX-R3 | save_config失败后内存与磁盘不一致（无回滚） | handler.py:788-796 |
| M10 | CX-R3 | _serialize_providers不序列化enabled/discovery/routing字段 | cloud_discovery.py:277 |
| M11 | CX-R2 | /api/metrics路径匹配用self.path而非parsed path，带query string时404 | handler.py:95 |

## 修复优先级

### P0 阻塞项（必须修复合入前）
1. **C1+C3**: RPM令牌泄漏 → 用Releasable句柄或统一acquire/release契约
2. **C2**: cloud双写日志 → 改为forward_to_cloud后单次log
3. **C7**: 加flush+fsync
4. **C9**: _providers修改移入_models_lock内

### P1 强烈建议（同批修复）
5. **C6**: 截断操作改为deque或swap-out模式
6. **C8**: 统一锁序（先save_lock再models_lock）
7. **M4**: _load_price_config改为惰性调用
8. **M11**: /api/metrics路径匹配修复

### P2 可后续迭代
9. C4, C5, M1-M3, M5-M10, L级问题
