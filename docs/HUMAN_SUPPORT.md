# 真实人工接管与工单

本模块在现有 FastAPI / React 项目中实现人工工作台。会话控制、消息、工单和业务操作记录持久化在同一数据库；人工消息不经过 LLM。

## 使用与部署

1. 为用户、客服、管理员配置现有的 `API_AUTH_USERS` 登录身份，配置真实的 `API_JWT_SECRET`。角色取自数据库，不信任前端或 JWT 自报角色。
2. `SUPPORT_DATABASE_URL` 留空时复用 `API_SESSION_POSTGRES_DSN`。所有 API 与 Worker 必须使用同一个 PostgreSQL 数据库。仅本地单进程演示可以显式配置 `sqlite:///data/support.db`。
3. 先备份，再运行 `python -m scripts.init_support_db` 创建新增表。初始化不会修改原有订单、Case、会话表；首次使用也会自动初始化。PostgreSQL 建表以事务级 advisory lock 串行化。正式发布后变更表结构须增加版本化迁移，不能仅修改 `CREATE TABLE IF NOT EXISTS`。
4. 配置首位管理员：`python -m scripts.support_staff manager admin`。配置客服：`python -m scripts.support_staff agent1 agent`。用户名必须对应已有登录身份。后续管理员可在工作台启用、停用、授权客服；这些操作会留审计记录。
5. 前端 `VITE_API_BASE_URL` 指向实际 API（默认 `http://localhost:8010`）；构建会据此生成 CSP。`CORS_ORIGINS` 配置前端来源。生产建议同域反向代理，`VITE_API_BASE_URL=/`，按部署设置 HTTPS Cookie。
6. 发布时停止旧版本接收请求，排空或人工核实旧任务，再同步升级 API 和 Worker。不要长时间混跑新旧版本：旧二进制无法识别本版本接管控制。新版本读取无 `control_version` 的旧排队任务时只授予初始版本 0，接管后不能重新执行。

启动方式和依赖沿用现有项目；无需新增第三方后端运行依赖。工单存储要求数据库可用，即使原有会话选择 JSON，也需要配置上述 PostgreSQL 或本地 SQLite。

用户点击“转人工客服”或发送明确指令“转人工”，即可进入队列；`requires_human` 结果和 `escalate_to_human` 工具同样创建实际工单。客服登录后点击“客服工作台”，领取工单后查看对话、交接摘要、工具查询依据及业务操作结果。管理员可转派和核实未知操作。

## 状态与边界

会话状态：`bot → handoff_pending → human`。用户明确取消排队或结束人工服务才返回 `bot`，接管和恢复都会增加 `control_version`。

工单状态：

| 操作 | 结果 |
| --- | --- |
| 转人工 | `queued`；重复请求复用当前工单 |
| 客服领取 / 管理员分配 | `in_progress` |
| 等待用户补充 | `waiting_customer` |
| 标记解决 | `resolved`，保持人工模式，不自动恢复机器人 |
| 等待/解决后用户继续发消息 | 当前工单重新进入 `in_progress`；负责人停用时重新排队 |
| 退回队列 | `queued`，解除负责人 |
| 用户取消排队 | `cancelled`，恢复机器人 |
| 用户结束人工服务 | `resolved`，恢复机器人 |

已结束工单只读，历史访问截至用户恢复机器人的事件序号。原负责人不能借旧工单看到后续新消息、工具依据或新工单；管理员仍需按所打开工单的历史边界查看。已转派客服的旧负责人立即失去访问权（已打开 SSE 会在下次检查时关闭）。队列预览不包含用户身份、完整对话或交接摘要。

内部备注、工具依据、业务核实记录只供获授权客服查看，不返回用户端，也不进入模型对话上下文。接管摘要由最近公开消息截取生成，不额外调用模型。原有会话首次导入时检查所有者，仅导入对用户可见的消息。新消息以 `support_events` 为准；旧会话表不再双写这些消息。重置人工会话被拒绝，用户可以新建咨询保留历史。

## 并发、幂等和故障处理

- PostgreSQL 会话行锁保护接管、抢单、消息序号分配与发布；工单 `version` 检查阻止过期操作；部分唯一索引阻止重复活动工单。锁只覆盖数据库事务，不跨 LLM 或下游网络调用。
- 用户请求 ID、客服消息 ID、执行占位和机器人结果均持久化。相同 ID 改变内容或确认参数返回 409。客户端重试复用原 ID；已完成的机器人请求返回保存的结果。已经开始却没有完成结果的任务不自动重跑，需查看会话或转人工。
- 接管立即改变控制版本。LLM 调用、ReAct 循环、工具执行和最终答复发布检查版本。外部调用无法强制撤销，但旧结果不能重新写入公开对话。原确认令牌绑定会话和控制版本，接管后失效。
- 写业务操作先写入 `running` 记录，再执行下游调用。若接管发生在下游已开始之后，原调用可以完成并记录结果，但不能继续启动后续业务写操作。
- 操作结果不明确时记录 `unknown` 并转人工，禁止自动重试。存在 `running/unknown` 操作时禁止恢复机器人。
- 若进程崩溃留下 `running`，管理员必须先确认原执行进程已经停止，再填写依据转为待核实；通过下游业务查询确认成功或未执行后才能恢复机器人。该按钮不会替管理员终止进程，也不会撤回已提交的操作。
- 人工核实只更新工单侧操作记录，不重放退款、不清空已有 Case/订单幂等记录。相同业务动作若仍被原有幂等保护锁定，应按业务系统的核实流程处理，不能靠新建工单绕过。
- 队列拒绝/连接失败不会释放已经开始执行的同 ID 任务。新控制版本使用新的 Redis 会话入队键，旧任务仍受数据库控制版本约束。
- PostgreSQL 提交失败必须向上抛出，不能假报成功；本次同时修复连接池吞掉提交异常及 MySQL DAO 归还连接前未提交的问题。

## 接口

接口沿用 JWT / Cookie 身份认证，Cookie 写操作要求 CSRF 令牌。

| 路由（前缀 `/api/v1`） | 用途 |
| --- | --- |
| `GET /support/me` | 当前角色 |
| `GET /support/staff`、`POST /support/staff/{user_id}` | 客服列表与管理员授权 |
| `POST /sessions/{id}/handoff`、`POST /sessions/{id}/resume-bot` | 转人工 / 恢复机器人 |
| `GET /sessions/{id}/support-state` | 当前控制状态 |
| `GET /sessions/{id}/messages?after=…` | 按序号补拉消息 |
| `GET /sessions/{id}/support-events?after=…` | SSE 更新与断线续传 |
| `GET /support/tickets?view=queue\|mine\|all&offset=…` | 分页队列 / 我的工单 / 管理员全部 |
| `GET /support/tickets/{id}` | 摘要、当前状态、业务操作、查询依据 |
| `POST /support/tickets/{id}/transition` | 领取、转派、等待、解决、退回 |
| `POST /support/tickets/{id}/messages` | 公开回复 / 内部备注 |
| `POST /support/operations/{id}/mark-unknown` | 管理员确认执行进程已停止 |
| `POST /support/operations/{id}/reconcile` | 管理员核实未知结果 |

客服读取消息/SSE 时使用 `staff=true&ticket_id=…`，服务端按该工单检查权限与历史边界。SSE 支持 `Last-Event-ID`；每批最多 100 个事件；游标不会跨过未返回的事件。

## 当前规模与后续扩展

本版本完成可运行的人工处理闭环和并发一致性保护，尚不能据此宣称达到“大公司高并发”容量：

- SSE 当前每 2 秒补查数据库，队列列表每 5 秒刷新。它支持跨进程补拉与断线恢复，但不是 Redis Pub/Sub 或事务 Outbox 推送架构。连接量扩大前需要增加事务 Outbox + 通知分发，保留数据库序号补拉作为兜底，并做连接数限制和慢消费者保护。
- PostgreSQL 行锁及唯一索引的设计需要在真实 PostgreSQL 上做多进程竞争、断连和故障恢复测试；SQLite 的通过结果不能替代这些测试。
- 尚无自动分配、客服容量调度、排班、SLA 超时告警、全文检索、附件、跨渠道消息、完整统计仪表盘。当前仅展示创建时间、负责人和首次人工回复时长。
- 消息与执行占位当前不自动清理；生产需要定义审计保留周期、归档和索引维护策略，再配合连接池、数据库与 SSE 指标进行容量压测。

部署前至少验证：真实 PostgreSQL + Redis 多实例抢单；接管与退款同时发生；Worker 被终止；浏览器断线补拉；停用/转派后 SSE 撤权；长连接与订单写入混合负载。按目标并发量测量 P95/P99、队列深度、DB 锁等待及错误率，再确定容量。
