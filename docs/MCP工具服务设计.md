# MCP 工具服务设计

## 能力与接入状态

项目提供基于 Streamable HTTP 的独立 MCP 服务、同步客户端、schema 转换和本地/远程工具管理器。MCP 服务复用当前业务工具及身份检查，不另建一套订单规则。

[scripts/task_worker.py](../scripts/task_worker.py) 的默认 `ConversationWorker → TaskWorker → TaskExecutor` 已接入 [事项工具分发](../app/tools/task_dispatch.py)。`MCP_ENABLED=false` 使用本地注册表；开启后，对当前事项允许的业务只读工具创建 `ToolManager`，远端已提供的同名工具走 MCP。事项流程、报价和准备确认工具继续本地执行，显式注入的测试执行器保持原行为。

Worker 启动校验配置。每次获准的远端读取独立签发短期用户上下文、创建连接并在 `finally` 关闭，不在进程或多个事项间共享用户连接。连接生命周期属于执行读取的受限线程；调用超时后由原线程清理，发现完成后再次检查租约和事项状态，禁止迟到的连接继续发起业务调用。每次调用重新签发令牌，长期运行的 Worker 不持有启动时签发的过期令牌。

## 远程暴露边界

[server.py](../app/mcp/server.py) 的 `list_tools` 从统一目录生成 JSON Schema，取全局白名单内且 `task_only=false` 的工具。目录使用 `effect != read` 或 `domain == flow` 判定事项专用能力，因此 `inspect_image` 即使是读取分析，也不能仅凭远程用户身份调用。

调用端直接请求未列出的事项专用工具同样会被拒绝，不能通过跳过发现接口访问报价、准备确认、提问或人工交接。`readOnlyHint` 只是协议提示，实际身份与对象归属仍由程序校验。目录及筛选规则见 [工具设计](工具设计.md)。

## 两级身份

每个 HTTP 请求携带：

1. `Authorization: Bearer <MCP_SERVICE_TOKEN>`：验证调用服务。
2. `X-MCP-User-Context`：短期签名用户上下文，核验用户、有效期、受众和作用域。

用户令牌由 [auth.py](../app/mcp/auth.py) 签发与验证，使用独立的 `MCP_CONTEXT_JWT_SECRET`，只在后端之间传递。Worker 从认证身份和可信渠道取得用户、`platform_code/store_id`，并把当前事项允许的业务只读工具名写入签名。服务端复核这些字段；发现与实际调用均取签名工具范围和服务端白名单的交集，业务服务继续按用户和渠道校验数据归属。它不同于浏览器登录令牌；模型不能自己传 `user_id` 或范围字段代替身份上下文。

服务端使用无状态 Streamable HTTP 与 JSON 响应，每个请求单独认证，避免复用首次建会话时的用户上下文。中间件将已核验身份、渠道范围和工具范围写入请求级 ContextVar，并在 `finally` 恢复。请求代表某用户，不等于自动取得其所有事项的执行资格。

## 客户端与工具管理

[MCPClient](../app/mcp/client.py) 在后台线程维护异步事件循环和 MCP 会话，向同步调用方提供 `connect`、`call_tool` 和 `close`。发现结果经 [converter.py](../app/mcp/converter.py) 转为 OpenAI function schema。

[ToolManager](../app/tools/manager.py) 只接受本地已登记且允许远程的工具名，继续使用本地参数定义；远端没有提供的工具由本地补齐，并与服务端明确允许的名称取交集。初始化连接失败时退回本地实现。

远程调用已经开始后发生错误，返回脱敏失败，由事项的有限重试预算处理。本轮曾使用远端的工具保持远端来源；后续重试即使发现失败或远端不再列出该工具，也不改成本地重放。初始化发现失败、尚未发起远程业务调用时可以使用本地同名实现。客户端对发现和调用施加超时，取消超时请求并关闭后台事件循环；不向模型或日志输出原始异常、服务令牌或用户令牌。

## 部署边界

`python -m app.mcp.server` 以带认证中间件的应用启动，当前绑定 `127.0.0.1:9123`。单独导出 FastMCP 内部应用不自动包含该身份中间件；不能按普通公共工具站点暴露。

启用时配置 `MCP_ENABLED=true`、`MCP_SERVER_URL`、`MCP_SERVICE_TOKEN`、`MCP_CONTEXT_JWT_SECRET` 与令牌有效期，并重启事项 Worker。客户端直接访问配置的服务地址，不继承环境 HTTP 代理；容器部署需使用可达的服务地址，当前独立服务默认仅绑定本机回环地址。

多进程的全局工具白名单、业务 DSN、业务密钥与 MCP 用户签名配置需保持一致。实际业务查询失败不能用远程发现成功替代。系统主链路、服务端认证与业务权限分别见 [系统架构设计](系统架构设计.md)、[认证与权限设计](认证与权限设计.md)。

## 验证入口

[test_mcp_worker.py](../tests/test_mcp_worker.py) 覆盖默认 Worker 的真实 HTTP 调用、并发用户与渠道隔离、签名工具限制、过期认证、发现失败降级、未知远端结果禁止本地重放、超时清理，以及开启 MCP 时本地确认流程和真实 MySQL 归属检查。
