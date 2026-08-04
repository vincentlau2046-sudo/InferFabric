# Bounded Context — IFF v4.6.5

## 核心限界上下文

### Metrics Context
- **职责**: 请求指标聚合、分位计算、API 暴露
- **核心实体**: MetricsAggregator, AggregatorThread
- **修复范围**: M-1 (空窗口 schema), M-2 (失败请求过滤)

### Dashboard Context
- **职责**: Web UI 渲染、数据展示
- **核心实体**: monitor.js, app.js, HTML fragments
- **修复范围**: D-1 (请求日志面板), D-2 (模型名映射)

### RequestLog Context (下游)
- **职责**: SQLite 持久化、查询
- **核心实体**: RequestLogDB
- **修复范围**: D-1 需要新增 /api/request_log 端点

## 上下文映射

```
Dashboard → /api/metrics → MetricsAggregator → Queue → RequestLogger → RequestLogDB
Dashboard → /api/request_log (新增) → RequestLogDB
```
