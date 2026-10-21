# 电商客服 Agent

一个以“事项”为单位处理对话的电商客服项目。用户可以同时咨询多个订单、连续补充消息，也可以在新的会话中继续之前的问题。系统使用一个 Agent 判断消息与哪些事项有关，再调用业务工具处理，由后台程序负责保存进度、校验权限、协调回复和恢复中断的任务。

后端采用 Python / FastAPI，前端采用 React / Vite，模型支持 DeepSeek 或其他 OpenAI 兼容接口。

## 能做什么

例如，用户先后发送：

> 帮我查一下耳机订单。
>
> 订单号是 ORD-20240120-002。
>
> 另外，那双鞋到哪里了？

系统先保存每条消息，再结合上下文判断：第二条是在补充耳机订单，第三条可能是另一个订单的物流问题。不同事项可以分别推进，无法确定“那双鞋”是哪笔订单时会追问。

| 场景 | 处理方式 |
| --- | --- |
| 一次对话涉及多个订单或问题 | 每个事项单独保存对象、进度、待答问题和处理结果 |
| 用户连续发送消息 | 持续接收，在处理时吸收已到达的补充信息，不预先假定用户会发几条 |
| 多个事项同时推进 | 独立事项可以并行，同一订单和业务写入受互斥与版本校验保护 |
| 新会话继续旧问题 | 在当前用户的历史事项中查找，确认归属后继续，并重新查询可能变化的业务状态 |
| 消息归属不明确或判断错了 | 先澄清；纠错后撤销错误关联的影响，使旧执行、旧确认失效 |
| 查订单、物流、商品和售后 | Agent 调用业务工具，由工具执行固定 SQL 或业务接口 |
| 提交退款等操作 | 先生成明确的操作方案，再由用户确认，服务端检查权限、参数与重复提交 |
| 需要人工处理 | 保存已有信息并移交人工，停止自动办理 |

收到几条消息，不等于必须回复几次。结果由回复协调器合并或分批发布；没有必要等待一个无法预知的“最后一条消息”。

## 一条消息怎样被处理

```text
用户发送消息
    ↓
API 校验身份，保存原话和请求编号，返回接收回执
    ↓
后台 Worker 读取待处理消息
    ↓
结合最近对话、待答问题和历史事项，判断消息归属
    ↓
校验关联结果：继续旧事项、创建新事项，或先追问
    ↓
Agent 调用允许的工具，读取结果，再决定下一步
    ↓
保存事项进度、工具依据和待确认操作
    ↓
回复协调器检查结果是否仍然有效，发布回复
    ↓
前端通过 SSE 接收事件，断线后补拉记录
```

事项上下文通过 `read_task_context` 读取，通过 `save_task_progress` 保存进度；工具依据统一保存在事项事件中。旧 Case 记忆及其存储已移除。

执行阶段使用 ReAct 工具循环：模型根据当前信息决定调用哪个工具，读取工具结果后继续查询、追问或结束。Text-to-SQL 工具已移除，数据库查询由业务工具执行固定 SQL。

围绕模型运行的这层控制程序就是 **Agent Harness**。它负责提供上下文、限定工具、设置超时与调用额度、保存结果，并在发布或提交前再次检查状态。用户身份、订单权限和操作确认都由服务端掌握，不能由模型自行决定。知识文本、用户输入和工具返回也不能改变这些权限。

如果执行期间收到纠正信息，旧结果不能直接继续发布；如果业务提交超时、结果未知，先查询回执或转人工，不能盲目重试。尚未实现的售后动作也不能被替换成另一种操作。

## 记忆、知识库和数据库

| 信息 | 用途 | 使用原则 |
| --- | --- | --- |
| 最近对话和待答问题 | 理解“这个订单”“是的”等省略表达 | 控制上下文长度，保留必要原话和出处 |
| 持久化事项记录 | 保存处理进度，支持跨会话继续 | 仅检索当前用户的记录；历史结果不能代替最新业务查询 |
| 用户长期记忆 | 保存有来源的偏好等稳定信息 | 不能作为订单归属、退款权限或金额依据 |
| 规则知识库（RAG） | 查找适用的退换货、配送等条款 | 按适用范围和有效时间筛选，回答保留来源 |
| 业务数据库 | 查询订单、物流、商品和售后状态 | 以工具查到的业务事实为准 |

数据分别保存在：

- **PostgreSQL**：消息、事项、执行记录、确认操作、用户记忆和人工处理记录。
- **MySQL**：订单、物流、商品及演示业务台账。
- **Redis**：任务唤醒、通知和共享额度控制。
- **Chroma**：知识向量索引；规则通过版本化索引发布、回退和撤回。

## 用 Docker 启动

需要 Docker Desktop。以下命令在仓库根目录执行，使用 PowerShell。

### 1. 准备配置和登录账号

```powershell
Copy-Item .env.example .env
docker compose build backend frontend
docker compose run --rm --no-deps backend python scripts/hash_password.py
```

密码脚本会交互式询问用户名和密码，并输出账号配置。演示时建议使用用户名 **`demo_user`**，因为内置订单属于这个用户。使用其他账号登录后，不能直接查询这些订单。

编辑 `.env`，至少核对以下配置：

| 配置 | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 有效的模型密钥；自动处理消息需要可用模型 |
| `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL_NAME` | 模型服务地址和该服务实际支持的模型名称 |
| `API_JWT_SECRET` | 自行生成的随机密钥，替换示例值 |
| `API_AUTH_USERS` | 密码脚本输出的账号及密码哈希 |
| `TASK_INTAKE_ENABLED` | 启用持续消息入口 |
| `TASK_WORKER_ENABLED` | 启用后台事项处理 |
| `TASK_OPERATIONS_ENABLED` | 启用业务操作确认入口 |

`API_AUTH_USERS` 的值要用单引号包住，防止 Docker Compose 把哈希中的 `$` 当作变量。例如：

```dotenv
API_AUTH_USERS='demo_user:这里替换为脚本输出的完整密码哈希'
```

不要将 `.env`、真实密钥或模型权重提交到仓库。

### 2. 启动服务

```powershell
docker compose up -d
docker compose ps
docker compose exec backend python -m scripts.pipeline_rollout status
```

Compose 会启动 API、Worker、回复通知进程、维护进程、前端和数据库，并运行演示订单初始化。访问：

| 地址 | 用途 |
| --- | --- |
| <http://localhost:5173> | 客服前端 |
| <http://localhost:8010/docs> | API 文档 |
| <http://localhost:8010/health> | 服务健康检查 |
| <http://localhost:8010/ready> | 就绪检查 |

如果使用已有数据库，自动处理可能处于暂停状态。确认这是本地演示环境、配置和数据正确后，可以启用：

```powershell
docker compose exec backend python -m scripts.pipeline_rollout set all --operator local-dev --reason "本地演示环境已核对"
```

生产迁移、按用户开放及恢复步骤见[运行手册](docs/CONCURRENCY_RUNBOOK.md)。

### 3. 验证一次对话

以 `demo_user` 登录，发送：

> 查一下 ORD-20240115-001 的物流。

再连续发送另一笔订单的问题和补充信息，观察消息接收、事项处理和最终回复。健康接口正常只说明服务可用，不代表模型调用或知识检索已经验证成功。

排查日志和停止服务：

```powershell
docker compose logs --tail 100 backend worker dispatcher
docker compose down
```

默认 Compose 的数据库配置用于本地开发，持久化数据保存在 `.docker-data` 等挂载目录；停止容器不会清空这些目录。

## 本地开发

建议使用 Python 3.11。前端需要 Node.js 20.19+ 或 22.12+，具体版本约束以依赖锁定文件为准。

只通过 Docker 启动数据库，再在本机运行应用：

```powershell
docker compose up -d postgres redis mysql
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

按上面的说明准备 `.env`；本地也可用 `python scripts/hash_password.py` 生成账号。模板中的 PostgreSQL、Redis 地址指向本机，MySQL 使用 **3307** 端口。初始化后端数据：

```powershell
python scripts/init_order_db.py
python -m scripts.init_support_db
```

在已激活虚拟环境的不同终端中分别运行：

| 进程 | 命令 |
| --- | --- |
| API | `python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8010` |
| 事项 Worker | `python -m scripts.task_worker` |
| 回复通知 | `python -m scripts.notification_worker` |
| 后台维护 | `python -m scripts.platform_maintenance` |

前端另开终端：

```powershell
cd frontend
npm ci
npm run dev
```

只启动 API，消息可以被接收，但后台事项不会自动完成。本地意图分类模型不是运行主链路的必需项，默认保持 `INTENT_MODEL_ENABLED=false`；这不代表自动办理可以不配置对话模型。

## 演示数据与规则知识库

### MySQL 演示数据

`scripts/init_order_db.py` 创建订单、物流和商品表，并写入固定演示记录。重复运行会更新对应的演示记录，适合本地开发库。

| 订单号 | 演示内容 |
| --- | --- |
| `ORD-20240115-001` | Nike 鞋，已发货，可查询物流 |
| `ORD-20240120-002` | AirPods 等商品，待发货 |

需要额外的优惠、发票等演示台账时运行：

```powershell
# 本地 Python 环境
python scripts/init_commerce_db.py --seed-demo

# 或者在已启动的 Docker 环境中运行
docker compose exec backend python scripts/init_commerce_db.py --seed-demo
```

这个脚本补充缺失的演示台账，不覆盖已有记录。部分演示报价和规则有有效期，过期后应检查数据。工具是否可被 Agent 使用，还取决于当前事项的工具白名单；建好表不代表所有动作都会开放。

### Markdown 规则与 RAG

[知识目录](app/rag/knowledge)包含退换货政策、配送说明、会员权益和常见问题。

启用规则问答需要完成：

1. 核对文档内容，在每份 `名称.md` 旁准备 `名称.policy.json`，填写来源、版本、适用店铺、渠道、品类、生效时间和审核状态。
2. 配置 Embedding 模型。默认使用本地模型，需要自行准备权重；也可以配置兼容服务的 Embedding 接口。对话模型的密钥不一定能用于向量生成。
3. 发布索引，使当前规则版本正式可检索。
4. 用匹配的业务条件验证命中条款、来源和适用时间。涉及订单时，业务数据还必须提供可靠的店铺、渠道、品类及时间信息。

元数据格式和发布约束见[规则检索方案](docs/refactoring/11-policy-retrieval.md)。准备完成后，在运行应用的环境中执行：

```powershell
python -m app.rag.commands.policy_index publish
```

本地向量模型由 `LOCAL_EMBEDDING_MODEL_PATH` 指定；Docker 默认将 `INTENT_MODEL_DIR` 对应的主机目录（默认 `./model`）挂载到 `/app/model`。使用远程服务时配置 `EMBEDDING_PROVIDER=openai`、`EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL` 和 `EMBEDDING_MODEL`。完整选项见[配置模板](.env.example)。

Docker 镜像会复制知识目录；修改资料后需要重新构建相关镜像，并在容器内执行发布命令。缺少审核资料、索引、向量模型或适用条件时，规则检索会明确失败，不应把失败解释为“没有这条限制”。

## 主要接口

接口以服务端认证身份隔离用户数据；调用方不能通过传入其他用户编号取得订单或事项。

| 接口 | 用途 |
| --- | --- |
| `POST /api/v1/auth/login` | 登录 |
| `POST /api/v1/task-messages` | 提交消息，返回 HTTP 202 接收回执 |
| `GET /api/v1/task-messages/{session_id}` | 查询消息处理进度 |
| `GET /api/v1/sessions/{session_id}/transcript` | 读取会话记录 |
| `GET /api/v1/sessions/{session_id}/support-events` | SSE 接收公开会话事件 |
| `GET /api/v1/task-operations/{session_id}` | 查看待确认操作 |
| `POST /api/v1/task-operations/{operation_id}/confirm` | 确认指定操作 |
| `POST /api/v1/sessions/{session_id}/handoff` | 转人工 |

提交消息需要 `session_id`、`request_id` 和 `message`。网络重试同一条消息时复用原 `request_id`，新消息使用新的编号。旧的 `/api/v1/chat` 提交、任务状态和事件接口均返回 410，客户端使用持续消息入口和会话事件。

## 测试

后端统一检查入口会隔离测试状态，避免依赖个人 `.env` 中的业务连接：

```powershell
python scripts/run_platform_checks.py
```

需要验证真实 PostgreSQL、MySQL 和 Redis 行为时，使用独立测试环境：

```powershell
docker compose -p ecom-refactor-tests -f deploy/compose.integration.yml up -d --wait
python scripts/run_platform_checks.py --integration
```

前端在 `frontend` 目录执行：

```powershell
npm ci
npm test
npm run test:browser
npm run build
```

浏览器测试需要 Playwright 可用浏览器；Windows 默认使用 Edge，其他环境按配置准备 Chromium。模型服务可用性、真实业务接入和规则检索效果，需要单独做端到端验证，不能只用离线测试通过来判断。

## 代码与文档入口

| 目录 | 主要职责 |
| --- | --- |
| `app/tasks` | 消息归属、事项执行、回复协调与操作确认 |
| `app/agents`、`app/execution` | Agent 工具循环与执行控制 |
| `app/tools` | 订单、物流、知识等业务工具 |
| `app/memory` | 用户记忆与相关存储 |
| `app/rag` | 知识切分、检索和索引发布 |
| `app/support` | 持久化记录、人工协作和运行状态 |
| `app/api` | HTTP 接口、认证和事件流 |
| `frontend` | 客服交互页面 |
| `scripts`、`deploy` | 初始化、后台进程、检查与部署配置 |
| `tests` | 自动化测试 |

