# Ecom Customer Service

一个面向电商客服场景的 Agent 服务，提供订单、物流、商品、退款和知识库问答能力。后端使用 FastAPI，执行层支持确定性的 Workflow 与带工具调用的 ReAct，前端使用 React/Vite。项目可以接入 DeepSeek 或其他 OpenAI 兼容模型；本地意图模型和 Embedding 模型均作为可选外部依赖，模型权重不会提交到仓库。

## 功能

- 意图识别、实体抽取和查询改写；没有本地模型时自动使用规则回退。
- 订单、物流、商品、退款、知识库和用户记忆工具。
- Workflow 的确认门控、能力降级和可验证证据；ReAct 的按意图工具裁剪。
- 短期上下文按任务装配：当前任务状态、近期原话、后台结构化摘要与历史回查；模型请求统一限制输入预算。
- FastAPI API、JWT/HttpOnly Cookie 登录、PostgreSQL 会话、Redis 队列与独立 Worker。
- MySQL 订单数据访问层、Chroma RAG、MCP 工具服务和 React 客服工作台。

业务扩充方案见 [电商客服业务场景扩充评审稿](docs/BUSINESS_SCENARIOS_REVIEW.md)：规划 28 个业务意图，加问候、收尾、其他共 31 类；逐类确定默认执行路径（12 类 ReAct、19 类 Workflow），包含原 29 类映射、48 个业务场景及 24 个多轮对照用例。本分支已更新 31 类识别契约、规则、提示词与路由；5 个旧枚举仅供历史任务兼容。19 类 Workflow 的完整步骤、分支、工具契约见文档专节，新增业务工具已有 MySQL 实现；Workflow 执行器仍需逐项接入。

## 架构

~~~text
React/Nginx -> FastAPI -> IntentSpec/DataPlan -> Workflow 或 ReAct
                                  |                 |
                                  +--> Tool/Evidence/Response
                                  |
                 PostgreSQL（会话/记忆） Redis（队列/缓存） MySQL（订单）
~~~

主要代码位于 app/：

~~~text
app/
├── api/          FastAPI 路由、认证和健康检查
├── agents/       Agent 入口和响应结构
├── data/         订单 DAO、缓存和数据适配
├── data_plan/    IntentSpec 与工具能力规划
├── execution/    Workflow、ReAct 和 dispatch
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

旧 17 类训练/评测数据保留为历史基线，不能直接用于新版训练：

- data/fine_tuning/multiturn_intent_sft/data_v3.jsonl
- data/fine_tuning/multiturn_intent_sft/data_v3_sharegpt.jsonl
- data/evaluation/multiturn_intent_eval_v3.jsonl

当前分类器采用 `ecom-intent-v4-31` 契约：结合多轮历史，**只识别最后一条用户输入当前提出或继续推进的一个意图**。每次输入聚焦一个订单、订单商品或完整咨询目标；推荐、比价等售前咨询不要求虚构订单。多订单拆分及状态机不属于本次训练任务。完整 31 类描述由 `app/data_plan/taxonomy.py` 注入 `app/prompts/intent_system.py`，训练和推理共用。仅输出 `{"intent":"refund_service"}`，不预测订单号、任务列表、解释或置信度。

目前按用户要求改为**从已有公开多轮对话提取后逐轮标注**，不使用种子集或补写历史。

当前已提取 **CSDS 的 9,910 段买家多轮对话**，保持原 train/val/test 分集（8,413/750/747），另生成 112,752 个待标注用户轮次索引。仅保留对话原文、角色、轮次和来源，未使用摘要补写对话。**尚未打意图标签，不能直接用于微调。** 统计、数据文件及复现方式见 [CSDS 提取说明](data/fine_tuning/csds_extracted/README.md)。

已核查 ECom-Bench 官方仓库版本 `bc5018daaf45eea12330941bce68cebd293cfa85`：公开内容包含任务定义、模拟器和业务数据库，没有找到可直接提取的对话日志。论文附录 A.2/A.3 有两段已公开的英文模拟轨迹，已提取为 **9 个多轮前缀，覆盖 4/31 类，仅 1 个独立任务组**。这是该 ECom-Bench 版本可取得材料的提取结果，独立于上述 CSDS 数据，不足以建立正式 train/dev/test。

- [原文与标注说明](data/fine_tuning/ecom_bench_extracted/README.md)
- [9 条逐轮标注](data/fine_tuning/ecom_bench_extracted/annotated_pool.jsonl)
- [原始对话](data/fine_tuning/ecom_bench_extracted/source_dialogues.json)
- [来源审计与缺失类别](data/fine_tuning/ecom_bench_extracted/source_audit.json)

样本保留截至当前用户轮的历史，`expected_intent` 仅标最后一轮；标注理由和来源不进入模型输入。相同任务的两段轨迹及其前缀不能跨集合，因此全部保持 `split=unassigned`，没有凑数生成 train/dev/test。原文为英文，尚未经过独立标注审核，也没有训练或评测真实模型。

~~~powershell
python scripts/extract_ecom_bench_dialogues.py --download
python scripts/extract_ecom_bench_dialogues.py --check
python scripts/eval_intent_model.py `
  --eval-file data/fine_tuning/ecom_bench_extracted/annotated_pool.jsonl `
  --validate-only
~~~

**旧 527 条改编种子已弃用**：`multiturn_intent_sft/train_v4.jsonl`、`dev_v4.jsonl`、`test_v4.jsonl` 及 `seed_dialogues_v4.txt` 仅保留历史，不用于本轮训练或验收。`scripts/build_intent_datasets.py` 是旧数据复现脚本，本轮不运行。`eval_intent_model.py` 现要求显式指定 `--eval-file`，不再默认加载旧种子集。

正式训练仍需补充可追溯的已有对话并完成 31 类覆盖、任务/会话级隔离和标签复核。得到合格 train/dev/test 后，训练使用 train，选模使用 dev，最后再评测 test；不将 test 传给训练的 `--eval-file`。0.5B 是否够用需据独立中文测试集的 macro-F1、逐类召回和混淆项判断，当前 9 条不能支持这一结论。

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

MySQL 测试使用随机订单/用户和独立记录，结束后按精确 ID 清理，不修改已有业务订单。详细工具路径、契约和实现范围见 [业务场景评审稿](docs/BUSINESS_SCENARIOS_REVIEW.md)。

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

短期记忆设计、数据边界和回归方案见 [短期上下文方案](docs/SHORT_TERM_MEMORY.md)。默认启用 `SHORT_TERM_MEMORY_ENABLED`，每次模型请求的估算输入上限由 `CONTEXT_REQUEST_INPUT_TOKENS` 控制（默认 12000，包含工具定义）。`CONTEXT_MODEL_WINDOW_TOKENS` 按实际提供商模型配置后，会额外扣除输出预算与安全余量。

后台摘要复用 Docker 的 `maintenance` 服务；本地可执行 `python -m scripts.platform_maintenance --once`。摘要模型调用在数据库事务之外；没有模型配置或摘要失败时仍可使用近期对话、任务状态和历史检索。摘要与模型窗口不会删除公开聊天记录。

长期记忆已接入公开用户事件、后台维护和认证请求。公开原话异步提取为有来源的用户事实，同一用户的新会话可以通过自动上下文或 `recall_user_memory` 召回。`MEMORY_ENABLED` 控制读写，`MEMORY_CONTEXT_TOKENS` 默认 1200；提取默认防抖 30 秒，实际延迟还取决于维护进程轮询和积压。Docker 的 `maintenance` 与 API/Worker 使用同一个长期记忆存储。

接入细节、验证边界及后续原子事实提取、冲突更新、冷热压缩和混合检索方案见 [长期记忆升级方案](docs/LONG_TERM_MEMORY.md)。文档明确区分本轮接通的功能与后续算法计划。

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

~~~

发布前确认以下内容不存在于提交中：.env、API Key、JWT/MCP 密钥、模型权重、日志、会话/记忆、数据库文件、个人路径和真实客户数据。如果曾经把密钥提交过，请先撤销并重新生成，再整理 Git 历史。

项目尚未声明开源许可证；公开发布前请根据你的用途补充合适的 LICENSE 文件。
