# 并发平台运行手册

本分支将受理、投递、执行、推送和维护分别部署。PostgreSQL 保存业务事实与执行记录，Redis 负责调度、共享额度和通知。测试只使用独立测试库、Redis namespace 和合成订单。

## 进程与扩容

| 角色 | 入口 | 扩容依据 |
|---|---|---|
| API | `uvicorn app.api.main:app` | 受理延迟、在线 SSE、连接池等待 |
| Dispatcher | `python -m scripts.chat_dispatcher` | 最老等待年龄、投递重试、通知积压 |
| Worker | `python -m scripts.chat_worker` | 执行耗时、排队年龄、可用模型额度 |
| 维护 | `python -m scripts.platform_maintenance` | 已完成任务体积；默认每 300 秒归档最多 200 条 |
| 检索 | `uvicorn app.services.retrieval:app --port 8020` | 检索耗时、共享槽位、CPU/GPU 占用 |

Compose 由 gateway 对外提供原来的 8010 端口，backend 不绑定宿主机固定端口，可水平扩容。入口仍是前端 5173、API 8010。扩容例子：

```sh
docker compose up -d --scale backend=2 --scale worker=2 --scale dispatcher=2
docker compose exec gateway nginx -s reload
```

nginx 在启动/重载时解析 backend 的多个地址；每次副本数或容器地址变化后重载。SSE 禁用代理缓冲，客户端断线后携带 Last-Event-ID 补拉。跨主机部署使用同一个 PostgreSQL、同一个逻辑 Redis、相同队列前缀/额度配置；Case 使用 PostgreSQL。JSON 模式只适用于本地开发。

示例 Compose 仍是开发部署：数据库密码、端口暴露、TLS、备份和基础设施高可用应由部署环境配置。单机 Redis 与 Redis 高可用部署可以使用当前客户端；不宣称支持 Redis Cluster 多分片，现有跨键 Lua 没有实现 Cluster hash-slot 分区。

## 升级和旧数据

1. 备份 PostgreSQL 和原 Case 目录；停止接收新请求并停止旧 API、Worker、Dispatcher。
2. 保留相同数据库和队列前缀。设置 PostgreSQL 连接后运行 `python -m scripts.init_support_db`。当前 schema version 为 7，采用增量建表/索引，并为旧任务消息关联补数据。迁移可能持有 DDL 锁，升级阶段执行，不在业务流量中启动多个新旧版本混跑。
3. 若原 Case 使用 JSON，先运行下面的验证，再执行导入。导入以用户为事务单位；相同数据可重跑，已有不同状态会拒绝覆盖。源文件始终保留。导入必须在所有 Case 写进程停止后进行。
4. 设置 `CASE_STORAGE_BACKEND=postgres`，启动 Dispatcher、Worker、API 和网关；观察 ready、指标与队列年龄后恢复流量。

```sh
python -m scripts.migrate_cases_to_postgres --source data/sessions/cases
python -m scripts.migrate_cases_to_postgres --source data/sessions/cases --apply
```

切换共享存储后，回滚到只支持 JSON 的版本前必须重新规划状态迁移，不能直接切回旧目录：新确认令牌和幂等结果已写入 PostgreSQL。旧版本任务已开始或写入结果未知时，保持人工核实流程。

## 额度和连接预算

完整配置样例见 `deploy/platform.env.example`。用户租户映射来自服务端 `API_USER_TENANTS`，不接受请求体指定租户。用户、租户、全局令牌桶原子扣减，拒绝一个维度时不会扣掉其他维度额度。这里是可突发的令牌桶，不是精确的滚动一分钟计数器。

- chat、read、control 分别计数；control/read 默认容量为聊天额度的 5 倍。取消、人工接管和人工消息不排队等待 Agent 槽位。
- Redis 故障时入站使用较低的进程级应急额度，数据库仍限制全局未完成任务数；应急额度不是全局额度，副本增加会扩大应急入口容量。Agent、模型和检索执行额度保持失败关闭。
- Agent 有全局和租户并发上限；模型有每模型并发、RPM、Token 预留预算；Embedding 使用独立额度，避免索引和检索挤占聊天预算。
- Token 预算以序列化输入 UTF-8 字节加协议余量、请求最大输出数做保守预留，失败不退款。它不是供应商的精确 tokenizer 计费；多模型共用供应商账户时还需按账户总额度配置服务网关。
- 默认 SSE 每用户共享上限 5，每 API 进程最多 2000。Redis 故障时每用户退回本进程限制，进程总限制始终有效；租约失效会结束旧连接。发送超过 10 秒断开，客户端可补拉恢复。
- PostgreSQL 连接池按“每进程、每 DSN”分配。例：2 API + 2 Worker + 2 Dispatcher + 1 维护进程，单池最大 10 时上限约 70；审计/记忆若使用不同 DSN，需要另外计算。预留管理连接，并观察池等待指标后调整。扩容 Worker 时同时核对模型额度与数据库总连接预算。

## 入队、重试、取消

202 表示消息、路由和执行任务已经在 PostgreSQL 同一事务提交。API 提交后退出不影响投递。Dispatcher 在数据库领取短租约，事务外提交 Redis，再确认投递；确认前崩溃允许重复投递，数据库执行占位防止重复执行。

尚未开始的任务可以重新投递；已经开始的任务不会自动重跑。执行期限默认 600 秒，超过期限会使旧会话控制版本失效并转人工。心跳丢失后，旧 Worker 在后续模型/工具调用和结果发布前停止；已经送到下游的网络请求不能靠取消撤销。退款等业务仍需下游幂等键/业务查询及人工对账，不能把队列机制解释成跨系统 exactly-once。

控制操作会改变会话版本，使旧任务进入 superseded。未消费的旧确认不能继续推进业务。用户改口、取消待办只取消后续处理，不撤销已提交业务。

## 推送与查询

通知 Outbox 与事件同事务落库；Pub/Sub 只通知会话发生变化，消息内容按数据库游标读取。通知按会话合并，每个 API 共用一个订阅线程，重复唤醒合并，定时补拉默认 15 秒。用户与客服均在每次补拉时验证权限；令牌到期关闭连接。

首次打开页面从 `/sessions/{id}/transcript` 获取最近 50 条消息、待办快照和一致的事件游标，不扫描全部历史；“查看更早消息”按 `before` 向前分页。后续 SSE 从快照游标继续。历史工单固定在结束人工服务的事件边界，不能看到之后的新消息。任务历史提供 `/sessions/{id}/tasks/history?before=...&limit=30`。

后台维护默认归档 30 天前的终态 Job payload/response，保留热表状态与执行占位。归档删除原始确认令牌，状态接口可以读取归档结果。未执行/运行中任务和业务幂等记录不删除。冷表保留审计数据，需要结合组织的数据保留政策安排导出与清理；本次不自动删除这些记录。PostgreSQL autovacuum 应开启，归档不等于立即把磁盘归还操作系统。

## 模型与检索独立部署

外部模型使用现有 `DEEPSEEK_BASE_URL`、`DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL_NAME` 指向兼容服务。`DEEPSEEK_BASE_URL` 必须包含服务实际使用的 `/v1` 路径。本地 vLLM 模板见 `deploy/compose.model.yml`：先设置经过目标 GPU 验证的固定镜像、模型权重目录、服务密钥，以及匹配模型的 `MODEL_TOOL_CALL_PARSER`，再独立启动。工具调用 parser 与 chat template 必须匹配模型。模板支持配置和接入，不代表当前机器已经验证 GPU 性能。

vLLM 支持 `VLLM_API_KEY` 服务鉴权，参见 [官方 Quickstart](https://docs.vllm.ai/en/latest/getting_started/quickstart/)。当前模板仅映射宿主机回环地址，容器中的 API/Worker 应使用部署网络中实际可达的模型地址。

启用 `remote-rag` profile，并在 API/Worker 配置 `RETRIEVAL_SERVICE_URL=http://retrieval:8020`，所有相关进程设置相同的至少 32 字符 `RETRIEVAL_SERVICE_TOKEN`。检索服务自身强制空 URL，避免递归。请求限制 query 4000 字符、top_k 1..5，客户端在下载时限制响应大小为 512000 字节，并检查结构。服务可用共享 Redis 并发额度拒绝过载请求。

当前向量库是嵌入式 Chroma：多主机检索副本应各自获得同一个经过版本校验的索引快照与相同 Embedding 模型；不要将本地目录当成分布式数据库。构建/更新索引应在离线目录完成，发布版本化快照后重启副本，避免在线全量删除重建索引。实际 GPU Embedding/LLM 性能需要在目标硬件上单独压测。

## 监控与处置

API `/metrics`、Worker 默认 9101、Dispatcher 默认 9102。后台端口在 Compose 内网暴露；每个进程单独作为 Prometheus target，不能把不同进程指标当作同一个累计计数器。启用 `monitoring` profile 可运行 Prometheus（本机 9090），DNS 服务发现跟随副本数变化；参考采集配置见 `deploy/prometheus.yml`，告警见 `deploy/platform-alerts.yml`。

| 指标/现象 | 处理 |
|---|---|
| `platform_oldest_waiting_job_seconds` 增长 | 检查 Dispatcher、Worker 就绪、资源额度及模型延迟，再考虑扩容 |
| `platform_jobs{status=...}`、投递重试 | DB 全局量在每个进程重复采集，使用 max；不能跨实例求和当成真实积压 |
| Worker/Dispatcher 心跳过旧 | 检查对应进程和 Redis；Worker 所有线程执行长任务时轮询心跳也会变旧，结合运行数量判断 |
| `platform_db_pool_timeouts_total` 增长 | 查慢 SQL、事务、连接预算；不要直接把每实例池调到很大 |
| 通知积压但消息仍能显示 | 游标补拉正在兜底，恢复 Redis/Dispatcher 后通知会继续 |
| interrupted 或未知业务 | 核实下游流水，确认旧进程已停止，由人工登记结果；不要重新投递退款 |
| Redis 重启/短暂中断 | 保留数据库；恢复连接后自动重投未开始任务；禁止清空共享 Redis |
| PostgreSQL 不可用 | 停止受理；修复数据库后验证 ready，不返回虚假的“已接收” |

直方图按各进程的 bucket 求和后计算 P95/P99；DB 全局 Gauge 用 max，SSE/连接池等进程 Gauge 可求和。告警阈值是初始示例，应根据业务 SLO 和实际运行超时调整。

## 可重复验证

```sh
python scripts/run_platform_checks.py
docker compose -p ecom-concurrency-test -f deploy/compose.integration.yml up -d --wait
python scripts/run_platform_checks.py --integration
python scripts/run_platform_checks.py --integration tests/test_platform_browser.py
npm --prefix frontend run build
```

集成依赖 psycopg、Redis、Chroma；浏览器验证还需要 Playwright 和 Chromium/Edge，非默认位置可设置 `ECOM_BROWSER_EXECUTABLE`。测试基础设施只绑定本机 25432/26379，数据库名固定 `ecom_concurrency_test`，每次使用新 schema 和 Redis namespace。日志、浏览器截图保留在 `.codex/platform-checks-*`，负载报告在 `.codex/concurrency-load-report.json`。完成测试后可停止专用 Compose 项目。

负载报告为小型回归证据，记录硬件、客户端数、受理/完成延迟和错误。合成模型与订单不会测出真实供应商限额、模型质量或 GPU 吞吐；上线前使用实际请求分布、目标并发和目标硬件补充容量测试。
