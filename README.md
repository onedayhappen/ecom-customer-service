# Ecom Customer Service

一个面向电商客服场景的 Agent 服务，提供订单、物流、商品、退款和知识库问答能力。后端使用 FastAPI，默认由统一对话 Agent 接待，按需调用工具；实际业务写入由确定性工具和服务端确认机制执行。前端使用 React/Vite。项目可以接入 DeepSeek 或其他 OpenAI 兼容模型；本地意图模型和 Embedding 模型均作为可选外部依赖，模型权重不会提交到仓库。

## 功能与链路

用户可以连续发送消息，每条消息先按请求编号保存原话并返回接收回执。一个统一 Agent 结合最近对话、已发布的问题、历史事项和有来源的记忆判断消息归属；关联不明确就澄清，关联错误可以撤销影响并重新判断。

事项分别保存进度，独立事项可以并行，共同订单和业务写入受到互斥保护。执行结果先保存，再由回复协调器合并或分批发布；接收消息的次数不决定回复次数。跨会话继续同一事项时复用记录，重新核实已经变化的业务状态。

退款等写操作先展示对象、动作和固定参数，再校验确认、权限、版本和幂等记录。超时结果未知时查询回执或转人工，不自动再提交。RAG 按店铺、渠道、商品及有效时间检索已发布条款；无依据时不编造规则。人工接管、纠错和暂停都会使旧执行与确认失效。

实现规则见 [完整方案](docs/AGENT_REFACTOR_PLAN.md)，各模块提交与验证见 [分步实施入口](docs/refactoring/README.md)。部署、迁移与恢复见 [运行手册](docs/CONCURRENCY_RUNBOOK.md)，目录用途见 [项目结构](docs/PROJECT_STRUCTURE.md)。

~~~text
React -> FastAPI -> 保存消息与接收位置（PostgreSQL）
                     ↓
             同一个 Agent 判断消息归属
                     ↓
             事项执行 / 查询工具 / 追问
                     ↓
             固定参数确认 -> 业务提交与回执
                     ↓
             回复协调 -> 公开会话事件 -> SSE / 游标补拉

PostgreSQL：原话、事项、执行、证据、确认、记忆与人工记录
Redis：唤醒、共享资源额度和通知；MySQL：订单与业务台账
~~~

`app/tasks` 管理上述过程，`app/agents` 和 `app/execution` 提供统一工具循环。分类器和训练工具保留为独立能力，31 类标签不承担多事项关联；原 Workflow、强制查询改写与单消息完整执行入口已经退役。

## Docker 部署

需要 Docker Desktop。复制配置模板并填写密钥：

~~~powershell
Copy-Item .env.example .env
# 编辑 .env，至少设置 API_JWT_SECRET、API_AUTH_USERS；自动办理还需设置模型 API Key
docker compose up -d --build
~~~

Compose 会启动 FastAPI、Agent Worker、React/Nginx、PostgreSQL、Redis 和 MySQL，并初始化演示订单数据。

- 前端：http://localhost:5173
- API：http://localhost:8010
- 健康检查：http://localhost:8010/health
- PostgreSQL：localhost:5432
- Redis：localhost:6379
- MySQL：localhost:3307

~~~powershell
docker compose ps
docker compose logs -f backend
docker compose down
~~~

数据库和运行时文件写入项目目录的 .docker-data/，该目录已被 Git 忽略，因此不会进入提交。若希望减少 C 盘占用，请把 Docker Desktop 的虚拟磁盘位置迁移到项目所在磁盘，并继续使用项目目录下的绑定目录。仓库不携带模型文件；INTENT_MODEL_ENABLED=false 时使用规则识别。

## 本地开发

需要 Python 3.11+、Node.js 20+、PostgreSQL、Redis 和 MySQL（或使用 Docker 提供数据库）。

~~~powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.api.main:app --reload --host 127.0.0.1 --port 8010
~~~

分别在独立终端启动事项 Worker、通知进程和维护进程：

~~~powershell
python -m scripts.task_worker
python -m scripts.notification_worker
python -m scripts.platform_maintenance
~~~

启动前端：

~~~powershell
Set-Location frontend
npm ci
npm run dev
~~~

自动归属与执行需要可用的模型服务；没有模型时只能使用历史读取和人工服务，不会以规则模式代办。需要本地意图模型或 Embedding 时，请把权重放在仓库外，并在 .env 中设置 INTENT_BASE_MODEL_PATH、INTENT_ADAPTER_PATH 或 LOCAL_EMBEDDING_MODEL_PATH；不要把权重复制回 Git 工作区。

## LoRA 意图分类器

当前分类器采用 `ecom-intent-v4-31` 契约：结合多轮历史，只识别最后一条用户输入当前提出或继续推进的一个意图。标签只作为 Agent 的参考，不承担多事项关联。完整 31 类描述由 `app/data_plan/taxonomy.py` 注入 `app/prompts/intent_system.py`，训练和推理共用；输出仅为 `{"intent":"refund_service"}`。

数据处理入口：

- [ECD 提取脚本](scripts/extract_ecd_dialogues.py)与 [CSDS 提取脚本](scripts/extract_csds_dialogues.py)：提取已有对话，提取结果需要完成标注与审核后才能训练。
- [训练脚本](scripts/train_intent_lora.py)：显式指定符合当前标签契约的训练集和验证集。
- [评测脚本](scripts/eval_intent_model.py)：通过 `--eval-file` 显式选择评测数据。
- [ShareGPT 导出](scripts/export_intent_sharegpt.py)：转换已标注的 train/dev/test，不生成新对话；[公共校验](scripts/intent_dataset_validation.py)检查标签覆盖、重复输入和来源交叉。

旧 17 类数据和改编种子不用于当前训练或验收，相关生成与修补脚本已移除。训练使用 train，选模使用 dev，最终评测使用 test；同一来源对话不能跨集合。模型是否满足要求以独立中文测试集的 macro-F1、逐类召回和混淆项为准。

原有 76 条规则回归样例继续单独使用：
`python scripts/eval_intent_model.py --rules --eval-file data/evaluation/multiturn_intent_eval_v4.jsonl`。

训练脚本检查角色顺序、单标签格式、train/dev 来源重叠并写入 `intent_taxonomy.json`；模型推理要求版本及标签集合匹配。旧 adapter 不匹配时回退规则。超过 `--max-length` 的样本报错，不会静默截掉最后一条用户消息。数据提取和格式检查不代表完成微调。

评测结果和训练日志不要写入 data/evaluation 的 jsonl 数据集目录，也不要提交模型权重。部署时把包含基座模型和 adapter 的外部目录设置为 INTENT_MODEL_DIR，并设置 INTENT_MODEL_ENABLED=true、INTENT_BASE_MODEL_PATH 和 INTENT_ADAPTER_PATH；未挂载时保持 false，服务使用规则回退。例如：

~~~powershell
$env:INTENT_MODEL_DIR = "D:/models/ecom-intent"
docker compose up -d --build
~~~

## 本地 MySQL 新增业务工具

已注册 17 项新增工具，并实现此前规划的 `calc_promotion`，共 18 项。它们覆盖报价、优惠规则、用户券、运费、历史价格、售后记录、发票台账、支付记录、订单操作资格、人工/操作状态，以及四项确定性校验与比较。金额使用整数分，用户身份来自服务端上下文。

使用 `.env` 中的 `ORDER_DB_DSN`。空库先运行已有的 `python scripts/init_order_db.py`；有基础业务表时直接运行：

~~~powershell
python scripts/init_commerce_db.py --seed-demo
~~~

新增 10 张 `commerce_*` 表，不修改原订单、商品或物流表。`--seed-demo` 根据现有商品和订单插入缺失的演示记录；省略该参数只建表。重复运行不会刷新或覆盖已有记录。演示报价/政策有效期为初始化后 90 天，过期后工具明确返回无有效依据，不自动延长。

演示报价支持现有商品 ID、数量 1、上海地区，渠道为 `pdd` 与 `demo_other_shop`；配送示例覆盖上海、杭州、北京。历史价格提供三个 `list` 口径样本，不声称完整历史覆盖。返回的演示记录明确标记 `mysql_demo` / `is_demo`。

开票与重开仅创建**本地演示台账**，不接税务系统、不生成正式发票。写操作必须经过服务端 `MutationGuard` 确认，再核验预览版本；数据库锁定订单并在同一事务中保存台账和幂等回执。查询、比较和金额估算均不提交交易。工具是否可调用仍由事项范围和服务端能力检查决定。

本地工具测试：

~~~powershell
python -m pytest tests/test_commerce_tools.py
$env:RUN_COMMERCE_MYSQL_TESTS = "1"
python -m pytest tests/test_commerce_mysql.py
~~~

MySQL 测试使用随机订单/用户和独立记录，结束后按精确 ID 清理，不修改已有业务订单。工具入口与参数契约见 [commerce.py](app/tools/commerce.py) 和 [commerce_models.py](app/tools/commerce_models.py)。

## 配置与认证

.env.example 只有占位值，真正的 .env 永远不要提交。常用配置：

- DEEPSEEK_API_KEY、DEEPSEEK_BASE_URL、DEEPSEEK_MODEL_NAME：业务模型，自动办理必须配置。
- API_JWT_SECRET：随机 JWT 密钥。
- API_AUTH_USERS：username:pbkdf2_sha256$...，可用脚本生成密码哈希。
- ORDER_DB_DSN：订单 MySQL 连接；为空时使用明确标注的演示 mock 数据。
- MEMORY_STORAGE_BACKEND、API_SESSION_STORAGE_BACKEND：postgres 或本地开发用 json。

生成登录用户哈希：

~~~powershell
python scripts/hash_password.py admin "your-password"
~~~

将输出内容写入 .env 的 API_AUTH_USERS。生产环境还应配置 HTTPS、限流、数据库备份和日志脱敏。

## API

默认启用 `SHORT_TERM_MEMORY_ENABLED`，每次模型请求的估算输入上限由 `CONTEXT_REQUEST_INPUT_TOKENS` 控制（默认 12000，包含工具定义）。`CONTEXT_MODEL_WINDOW_TOKENS` 按实际提供商模型配置后，会额外扣除输出预算与安全余量。

后台摘要复用 Docker 的 `maintenance` 服务；本地可执行 `python -m scripts.platform_maintenance --once`。摘要模型调用在数据库事务之外；没有模型配置或摘要失败时仍可使用近期对话、任务状态和历史检索。摘要与模型窗口不会删除公开聊天记录。

长期记忆已接入公开用户事件、后台维护和认证请求。公开原话异步提取为有来源的用户事实，同一用户的新会话可以通过自动上下文或 `recall_user_memory` 召回。`MEMORY_ENABLED` 控制读写，`MEMORY_CONTEXT_TOKENS` 默认 1200；提取默认防抖 30 秒，实际延迟还取决于维护进程轮询和积压。Docker 的 `maintenance` 与 API/Worker 使用同一个长期记忆存储。

当前实现见 [app/memory](app/memory/)。记忆、历史事项检索和纠错后的记忆清理统一按 [Agent 重构实施方案](docs/AGENT_REFACTOR_PLAN.md) 改造。

公开接口：GET /health、GET /ready、GET /metrics 和 POST /api/v1/auth/login。其余业务接口需要登录 Cookie 或 Authorization: Bearer <jwt>。

- `POST /api/v1/task-messages`：提交 `session_id`、稳定的 `request_id` 和 `message`，返回 202 接收回执；网络结果未知时使用同一请求编号重试。
- `GET /api/v1/task-messages/{session_id}`：查询接收及处理位置、自动办理是否暂停。
- `GET /api/v1/sessions/{session_id}/transcript`、`/support-events`：读取公开记录或订阅变化，断线按游标补拉。
- `GET /api/v1/task-operations/{session_id}`：获取操作卡片；使用 `/api/v1/task-operations/{operation_id}/confirm|cancel|refresh` 确认、取消或核查状态。
- `/api/v1/sessions/{session_id}/handoff`：转人工；人工工作台继续使用客服接口及数据库角色权限。
- 旧 `POST /api/v1/chat` 返回 410；历史 Job 状态和事件接口保留读取。旧 reset/close 写入口返回 409，保留审计记录，前端用新建会话开始下一次咨询。

新安装的四项 `TASK_*_ENABLED` 能力配置默认开启；已有数据升级后数据库自动办理状态为 `stopped`。上线以数据库中的用户放量控制为准，不用各实例不同的环境变量分流。执行 `python -m scripts.pipeline_rollout status` 查看状态，切换步骤见运行手册。

## 测试

~~~powershell
python scripts/run_platform_checks.py
Set-Location frontend
npm test
npm run test:browser
npm run build
~~~

发布前确认以下内容不存在于提交中：.env、API Key、JWT/MCP 密钥、模型权重、日志、会话/记忆、数据库文件、个人路径和真实客户数据。如果曾经把密钥提交过，请先撤销并重新生成，再整理 Git 历史。

项目尚未声明开源许可证；公开发布前请根据你的用途补充合适的 LICENSE 文件。
