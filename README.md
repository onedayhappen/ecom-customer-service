# Ecom Customer Service

一个面向电商客服场景的 Agent 服务，提供订单、物流、商品、退款和知识库问答能力。后端使用 FastAPI，默认由统一对话 Agent 接待，按需调用工具；实际业务写入由确定性工具和服务端确认机制执行。前端使用 React/Vite。项目可以接入 DeepSeek 或其他 OpenAI 兼容模型；本地意图模型和 Embedding 模型均作为可选外部依赖，模型权重不会提交到仓库。

## 功能

- 31 类业务标签只作参考，不锁定执行路径；没有本地分类模型时使用规则回退。
- 订单、物流、商品、退款、知识库和用户记忆工具。
- 自由多轮对话支持追问、插问和改口；不再自动拆待办或强制填槽。写操作先预检、展示具体内容，再校验确认令牌。
- 会话原话、后台摘要、历史回查和跨会话订单草稿；草稿使用版本校验，不能覆盖业务工具的权威状态。
- FastAPI API、JWT/HttpOnly Cookie 登录、PostgreSQL 会话、Redis 队列与独立 Worker。
- MySQL 订单数据访问层、Chroma RAG、MCP 工具服务和 React 客服工作台。

后续重构的总体规则见 [Agent 重构实施方案](docs/AGENT_REFACTOR_PLAN.md)，每次实现从 [分步实施入口](docs/refactoring/README.md) 选择当前小方案。本文描述当前运行方式；部署与故障处理见 [运行手册](docs/CONCURRENCY_RUNBOOK.md)，代码目录见 [项目结构](docs/PROJECT_STRUCTURE.md)。

当前分类器保留 28 个业务意图，加问候、收尾、其他共 31 类标签；原 Workflow / ReAct 分流已退出默认运行链路。分类器输出仍只有意图标签，训练数据格式不变。接待全部意图不代表所有业务动作都已实现；本地演示发票不连接税务系统。

## 架构

~~~text
React/Nginx -> FastAPI -> 统一对话 Agent -> 查询 / 计算 / 澄清
                           |          -> 操作预检 -> 参数绑定确认 -> 业务写入
                           |          -> 人工接管
                           +-- 会话上下文 / Case 草稿 / 工具证据
              PostgreSQL（会话/记忆） Redis（队列/缓存） MySQL（业务数据）
~~~

主要代码位于 app/：

~~~text
app/
├── api/          FastAPI 路由、认证和健康检查
├── agents/       Agent 入口和响应结构
├── data/         订单 DAO、缓存和数据适配
├── data_plan/    IntentSpec 与工具能力规划
├── execution/    对话工具循环、操作预检与历史 Workflow 回滚路径
├── intent/       意图分类、规则回退和查询改写
├── memory/       长期记忆与 Case
├── rag/          知识库和 Chroma 检索
├── session/      会话与上下文压缩
├── tools/        商品、订单、物流和退款工具
└── config/       配置模型
frontend/         React/Vite 客服界面
scripts/          Worker、订单初始化和密码哈希工具
tests/            自动化测试
~~~

## Docker 部署

需要 Docker Desktop。复制配置模板并填写密钥：

~~~powershell
Copy-Item .env.example .env
# 编辑 .env，至少设置 API_JWT_SECRET、API_AUTH_USERS；需要模型时再设置 API Key
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

另开终端启动 Worker：

~~~powershell
python -m scripts.chat_worker
~~~

启动前端：

~~~powershell
Set-Location frontend
npm ci
npm run dev
~~~

本地不需要模型即可运行规则模式。需要本地意图模型或 Embedding 时，请把权重放在仓库外，并在 .env 中设置 INTENT_BASE_MODEL_PATH、INTENT_ADAPTER_PATH 或 LOCAL_EMBEDDING_MODEL_PATH；不要把权重复制回 Git 工作区。

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

开票与重开仅创建**本地演示台账**，不接税务系统、不生成正式发票。写操作必须经过服务端 `MutationGuard` 确认，再核验预览版本；数据库锁定订单并在同一事务中保存台账和幂等回执。查询、比较和金额估算均不提交交易。新增 Workflow 的 `execution_ready` 保持原状态，本次完成的是工具层。

本地工具测试：

~~~powershell
python -m pytest tests/test_commerce_tools.py
$env:RUN_COMMERCE_MYSQL_TESTS = "1"
python -m pytest tests/test_commerce_mysql.py
~~~

MySQL 测试使用随机订单/用户和独立记录，结束后按精确 ID 清理，不修改已有业务订单。工具入口与参数契约见 [commerce.py](app/tools/commerce.py) 和 [commerce_models.py](app/tools/commerce_models.py)。

## 配置与认证

.env.example 只有占位值，真正的 .env 永远不要提交。常用配置：

- DEEPSEEK_API_KEY、DEEPSEEK_BASE_URL、DEEPSEEK_MODEL_NAME：业务模型，可留空运行规则模式。
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

- POST /api/v1/chat：提交客服消息；异步模式返回 job_id。
- GET /api/v1/chat/{job_id}：查询任务状态和回复。
- GET /api/v1/chat/{job_id}/events：SSE 事件流。
- GET /api/v1/sessions/{session_id}：读取会话。
- POST /api/v1/sessions/{session_id}/reset：清空会话。

## 测试

~~~powershell
python -m pytest -q
Set-Location frontend
npm run build
~~~

发布前确认以下内容不存在于提交中：.env、API Key、JWT/MCP 密钥、模型权重、日志、会话/记忆、数据库文件、个人路径和真实客户数据。如果曾经把密钥提交过，请先撤销并重新生成，再整理 Git 历史。

项目尚未声明开源许可证；公开发布前请根据你的用途补充合适的 LICENSE 文件。
