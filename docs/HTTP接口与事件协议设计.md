# HTTP 接口与事件协议设计

## 接口分组

API 负责认证、接收、查询、控制、确认及事件传输。模型执行由独立 Worker 推进，消息 HTTP 请求不等待整个 Agent 结束。应用与中间件装配见 [main.py](../app/api/main.py)，字段约束以对应 Pydantic 请求模型为准。

| 接口组         | 主要路径                                                                                             | 作用                                           |
| -------------- | ---------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| 登录身份       | `/api/v1/auth/register`、`login`、`logout`；`/api/v1/me`                                             | 注册、登录、退出与当前用户                     |
| 持续消息       | `POST /api/v1/task-messages`；`GET /api/v1/task-messages/{session_id}`                               | 持久化接收、幂等回执与关联进度                 |
| 会话读取       | `/api/v1/sessions/{session_id}/transcript`、`messages`、`tasks`、`tasks/history`                     | 分页历史、事件补拉及事项视图                   |
| 实时更新       | `GET /api/v1/sessions/{session_id}/support-events`                                                   | 按游标订阅 SSE                                 |
| 业务确认       | `/api/v1/task-operations/{session_id}`；`/{operation_id}/confirm`、`cancel`、`refresh`               | 卡片读取、固定确认、取消未开始操作与核实       |
| 人工控制       | `/api/v1/sessions/{session_id}/handoff`、`resume-bot`、`support-state`                               | 转人工、恢复与当前控制权                       |
| 客服工作台     | `/api/v1/support/me`、`staff`、`tickets`、`operations`                                               | 权限、领取、转派、回复、内部备注与未知结果处理 |
| 客户数据       | `/api/commerce/profile`、`orders`、`coupons`                                                         | 已鉴权的客户资料和业务列表                     |
| 发票进度与文件 | `/api/commerce/invoice-requests/{invoice_id}`；`/api/commerce/invoices/{issued_invoice_id}/document` | 本人开票申请进度及私有票据下载                 |
| 图片           | `/api/v1/attachments` 及附件编号下的子接口                                                           | 上传、鉴权读取、撤回与识别重试                 |
| 记忆归档       | `/api/v1/sessions/{session_id}/memory-archive`；`/api/v1/memory-archives/{archive_id}`               | 固定截止点并查询处理状态                       |

表中同组短路径均接在前述前缀后，不能把例如 `cancel` 当作无操作编号的独立入口。完整路由见 [support.py](../app/api/support.py)、[commerce.py](../app/api/commerce.py) 和 [task_operations.py](../app/api/task_operations.py)。

## 消息请求与幂等

消息请求包括 UUID 类型的 `session_id`、`request_id`，最多 4000 字符的 `message`、最多 4 个有序 `attachment_ids`，以及可选 `action`、`task_id`、`task_version`。文字、图片或合法控制必须提供有效输入；图片协议详见 [图片处理与多模态上下文设计](图片处理与多模态上下文设计.md)。

服务端在事务中保存公开原话、附件绑定、消息处理项和必要唤醒，返回 HTTP 202 与 `session_id/request_id/sequence/status=accepted`。`sequence` 是会话事件位置，不是“第几条用户消息”，中间可以存在其他事件。

请求去重范围是同一用户，而非单个浏览器会话。网络结果未知时复用原编号；同编号改变文字、图片顺序、会话或控制内容将被拒绝。接收成功不表示完成关联、回复或办理。

`action=cancel` 控制客服事项，不等于取消订单；订单取消须走业务确认。明确转人工可走控制入口，模糊的自然语言停止要求不能直接撤销任意事项。

### 请求与回执示例

以下使用示例 UUID。在已登录并满足 CSRF 校验的客户端中发送：

```http
POST /api/v1/task-messages
Content-Type: application/json

{
  "session_id": "11111111-1111-4111-8111-111111111111",
  "request_id": "22222222-2222-4222-8222-222222222222",
  "message": "查询订单的物流进度",
  "attachment_ids": []
}
```

接收成功返回 HTTP 202。以下假设该消息对应事件序号为 7：

```json
{
  "session_id": "11111111-1111-4111-8111-111111111111",
  "request_id": "22222222-2222-4222-8222-222222222222",
  "sequence": 7,
  "status": "accepted"
}
```

`GET /api/v1/task-messages/{session_id}?after=0` 查询接收与关联位置，返回 `received_sequence`、`associated_sequence`、`messages`、`automatic_processing` 和 `processing` 等字段。两个序号分别是已接收位置和已完成关联的位置；关联完成后仍可能存在执行或待发布回复。

| `processing` | 服务端判断 |
| --- | --- |
| `processing` | 有未关联输入或活跃执行；或者没有人工/用户等待，但仍有待消费执行结果 |
| `waiting_human` | 没有未关联输入及活跃执行，且事项仍由人工接管 |
| `waiting_user` | 没有未关联输入、活跃执行或人工接管，且存在待用户回答的事项或关联澄清 |
| `idle` | 当前没有上述工作或等待 |

这些值是查询时的流程视图，不是单向状态机；新增消息或人工控制可以改变下一次读取结果。

## 历史快照与 SSE

1. 客户端读取 `transcript`，取得可见历史、分页信息及一致的事件游标。
2. 使用 `support-events?after=游标` 订阅；重连可携带 `Last-Event-ID`，服务端取两者较大值。
3. SSE `event: update` 的 `id` 是新游标，`data` 包含数据库补拉得到的事件与状态。
4. 游标没有变化时发送注释心跳；服务端给出随机 `retry` 间隔提示。
5. 登录过期或访问资格失效发送 `event: revoked` 并结束；共享连接租约丢失也结束连接。

Redis 通知只唤醒查询，不携带唯一的消息事实。定时数据库补拉覆盖通知遗漏；客户端以稳定编号、序号与覆盖范围合并，不能用“最后到达的响应”覆盖新状态。SSE 的 `id` 是批次游标，不代表每次推送恰好只有一条事件。

客服读取携带 `staff` 与 `ticket_id` 时仍由服务端检查角色、负责范围和工单历史上限。内部事件不会因为共用连接协议而变成客户可见内容。

### 事件字段与推送示例

事件行以 `session_id + sequence` 标识，包含 `event_key`、`kind`、`actor`、`visibility`、`payload` 和 `created_at`。`payload` 在 API 返回时已解析为对象；客户端按 `kind` 解释，按会话和序号去重。

| `kind` 示例 | 载荷含义 | 产生位置 |
| --- | --- | --- |
| `message` | `role/content` 及请求、附件或事项等关联字段 | 消息接收、回复发布、人工回复 |
| `support_state` | 控制模式、工单状态及相应控制信息 | 转人工、领取与恢复 |
| `operation_prepared` | `operation_id/task_id/task_version`，内部事件 | 准备业务操作 |
| `operation_started/operation_receipt` | 操作开始及提交结果，内部事件 | 用户确认与业务提交 |
| `task_operation_result` | 操作回执及对应事项版本，内部事件 | 业务回执落库 |

上表也列出存储层的内部事件，客户 SSE 会过滤它们。确认卡片通过已鉴权的操作查询接口读取，不从内部事件载荷提取确认令牌。

以下表示客户端已读取到序号 6，随后收到一批包含序号 7 的更新；示例仅投影 `message.payload` 的核心字段，真实载荷还带请求及控制关联字段：

```text
retry: 3000

id: 7
event: update
data: {"events":[{"session_id":"11111111-1111-4111-8111-111111111111","sequence":7,"event_key":"intake:22222222-2222-4222-8222-222222222222","kind":"message","actor":"example-user","visibility":"public","payload":{"role":"user","content":"查询订单的物流进度"},"created_at":"2026-10-30T08:00:00+00:00"}],"cursor":7,"state":{"session_id":"11111111-1111-4111-8111-111111111111","mode":"bot","control_version":1,"cursor":7,"ticket":null}}

: heartbeat

```

SSE `event` 是传输类型，JSON 内的 `kind` 是业务事件类型。即使这一批没有客户可见事件，游标也可能跨过内部事件而推进。`transcript` 的 `next_before` 用于向前读取旧消息，`cursor` 用于从快照位置继续订阅，两者不能互换。前端合并实现见 [conversationState.js](../frontend/src/conversationState.js)。

## 开票进度与文件

`GET /api/commerce/invoice-requests/{invoice_id}` 核验登录用户及申请归属，返回申请状态和已开票据摘要，包含演示来源及文件可用性。确认卡片据此轮询后续开票进度；原业务操作提交成功不表示票据已经生成。

文件下载按 `issued_invoice_id` 再核验本人申请、文件状态、私有路径范围及摘要。接口不接受任意文件 URL，模拟 PDF 明确标为演示票据；文件生成与恢复见 [本地模拟渠道设计](本地模拟渠道设计.md)。

## 错误、健康与兼容边界

`SupportError` 返回 `{"detail":"…"}` 及相应状态码；请求模型错误使用 FastAPI 校验结构；额度不足返回 429 和 `Retry-After`，共享协调不可用返回 503。业务工具的 `success/error_code` 是工具结果协议，不能与所有 HTTP 响应格式混用。

`/health` 只说明 API 进程可响应；`/ready` 检查支持库中放量版本是否匹配，自动处理暂停时仍可就绪。它不证明 MySQL、模型、视觉端点和所有 Worker 已可用。`/metrics` 提供指标，具体暴露边界见 [认证与权限设计](认证与权限设计.md)。

旧 `/api/v1/chat` 系列返回 410，不存在可继续使用的第二条聊天执行链。旧会话 `reset/close` 路由保留拒绝入口；开启新咨询、人工恢复和记忆归档分别处理，不能用关闭会话替代业务结束。

## 代码入口

- [API 装配](../app/api/main.py)、[认证中间件](../app/api/middleware.py)。
- [消息契约](../app/api/task_messages.py)、[事务接收](../app/tasks/intake.py)。
- [历史与 SSE](../app/api/support.py)、[事件存储](../app/support/store.py)。
- [连接限额](../app/runtime/streaming.py)、[前端请求与订阅](../frontend/src/api/client.js)。
