# 并发平台验收记录

分支：`codex/concurrency-platform`；基线：`35f5232`。完整范围见 `CONCURRENCY_PLATFORM.md`，部署与故障处置见 `CONCURRENCY_RUNBOOK.md`。

## 实现

- PostgreSQL 事务 Outbox、并行投递租约、运行占位、任务状态/结果、执行期限与未知业务恢复；Redis 残留由数据库终态驱动收敛。
- 通知 Outbox、跨 API 唤醒、游标补拉、鉴权检查、SSE 连接额度、慢消费者与断连清理；任务结果接口也使用事件唤醒。
- 全局/租户/用户入站额度，独立控制通道，Agent/模型/Embedding/检索资源预算；租约失效阻断后续模型和业务调用。
- PostgreSQL 读写锁分离、活跃任务索引、消息关联索引、消息和任务历史分页、工单结束快照、旧快照后台补齐、终态任务归档。
- Case、确认令牌、幂等状态使用共享 PostgreSQL；旧 JSON 导入工具拒绝覆盖不同状态；确认锁按对象与用户事务隔离。
- 独立检索服务与有界 HTTP 客户端、模型独立部署配置、可横向扩容的 API 网关、Prometheus 采集与告警配置。

## 验证证据（2026-10-13）

| 验证 | 结果 | 本地证据 |
|---|---|---|
| 完整离线回归 | 452 passed / 22 skipped；2 个旧测试 JWT 弱密钥警告 | `.codex/platform-offline-final.txt` |
| 真实多实例完整集成 | 24 passed；PostgreSQL 16、Redis 7、真实 Chroma；2 API、2 Dispatcher、2 Worker，额外 API 验证网络隔离和 SSE 额度 | `.codex/platform-integration-final.txt` |
| Review 最后一次锁范围调整 | 38 项 Case/业务幂等/资源/分页回归通过；10 项 PostgreSQL/Redis 集成通过，包括新增同进程跨用户不阻塞 | `.codex/platform-review-regression.txt`、`.codex/platform-review-integration.txt` |
| SSE 最终边界验证 | 2 passed；客服权限撤回、令牌到期、任务取消状态推送并结束连接 | `.codex/platform-review-sse.txt` |
| 真实浏览器 | 1 passed；用户和客服登录、提问续办、刷新恢复、退款确认、分页、人工接管、内部备注隔离、移动端布局，无 JS 异常 | `.codex/platform-browser-current.txt`；截图位于 `.codex/platform-checks-1oy0yxv7/tmp/platform-http0/` |
| 前端生产构建 | 通过，JS 217.48 KB、CSS 19.62 KB | `npm run build` |
| Compose | 主配置及 monitoring/remote-rag profiles、独立模型配置校验通过 | `docker compose config --quiet` |
| nginx 配置 | 官方 nginx:1.28-alpine 容器 `nginx -t` 通过 | `.codex/platform-nginx-check.txt` |
| Prometheus 配置 | 官方 promtool 验证成功，6 条告警规则 | `.codex/platform-prometheus-check.txt` |

离线跳过项为显式集成/浏览器场景；不能把 skip 计作通过。完整集成先于最后一次确认锁范围调整，随后执行了上表两组针对性回归；没有把针对性测试写成重新跑了整个集成套件。

负载为 24 请求、6 个闭环客户端，9.891 秒，0 错误，约 2.426 请求/秒；受理 P95/P99 为 1.078/1.094 秒，完成 P95/P99 为 3.563/3.656 秒。环境为 Windows 10、8 逻辑 CPU、Docker Desktop。分位数采用 nearest-rank；24 个样本的 P99 接近最大值，不能替代大样本容量测试。

这轮测试使用合成模型协议、订单和向量编码器；Chroma 索引与查询、HTTP、数据库和队列是真实实现。没有运行 GPU 模型或验证供应商容量。vLLM 配置通过静态校验，不构成目标 GPU 上的运行证明。

早期完整集成曾因故障注入后的 Worker 尚未就绪而超出等待时间，之后增加角色就绪确认，并持续保留独立日志；最新记录以上表为准。

原有 `README.md` 与 `docs/BUSINESS_SCENARIOS_REVIEW.md` 改动保留，不纳入本功能提交。
