# Ecom Customer Service

一个面向电商客服场景的 Agent 服务，提供订单、物流、商品、退款和知识库问答能力。后端使用 FastAPI，执行层支持确定性的 Workflow 与带工具调用的 ReAct，前端使用 React/Vite。项目可以接入 DeepSeek 或其他 OpenAI 兼容模型；本地意图模型和 Embedding 模型均作为可选外部依赖，模型权重不会提交到仓库。

## 功能

- 意图识别、实体抽取和查询改写；没有本地模型时自动使用规则回退。
- 订单、物流、商品、退款、知识库和用户记忆工具。
- Workflow 的确认门控、能力降级和可验证证据；ReAct 的按意图工具裁剪。
- FastAPI API、JWT/HttpOnly Cookie 登录、PostgreSQL 会话、Redis 队列与独立 Worker。
- MySQL 订单数据访问层、Chroma RAG、MCP 工具服务和 React 客服工作台。

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

训练集和独立评测集保存在仓库中：

- data/fine_tuning/multiturn_intent_sft/data_v3.jsonl
- data/fine_tuning/multiturn_intent_sft/data_v3_sharegpt.jsonl
- data/evaluation/multiturn_intent_eval_v3.jsonl

当前代码和已有产物按 Qwen2.5-0.5B-Instruct（外部基座模型）配置；如果你手头使用的是 0.6B 兼容基座，只需替换 --model。adapter 和 checkpoint 输出到仓库外：

~~~powershell
pip install -r requirements-intent.txt
python scripts/train_intent_lora.py `
  --model Qwen/Qwen2.5-0.5B-Instruct `
  --train-file data/fine_tuning/multiturn_intent_sft/data_v3.jsonl `
  --output-dir ../intent-model/intent-lora
python scripts/eval_intent_model.py `
  --model Qwen/Qwen2.5-0.5B-Instruct `
  --adapter ../intent-model/intent-lora
~~~

评测结果和训练日志不要写入 data/evaluation 的 jsonl 数据集目录，也不要提交模型权重。部署时把包含基座模型和 adapter 的外部目录设置为 INTENT_MODEL_DIR，并设置 INTENT_MODEL_ENABLED=true、INTENT_BASE_MODEL_PATH 和 INTENT_ADAPTER_PATH；未挂载时保持 false，服务使用规则回退。例如：

~~~powershell
$env:INTENT_MODEL_DIR = "D:/models/ecom-intent"
docker compose up -d --build
~~~

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

测试数据和业务 mock 均为合成数据。评测运行产物、会话、向量索引和日志写入 data/ 或 logs/，这些目录已被忽略。

## 发布前检查

~~~powershell
git status --short
git diff --check
git grep -n -I -E "(sk-[A-Za-z0-9]{20,}|-----BEGIN (RSA|OPENSSH|EC|PRIVATE) KEY-----|AKIA[0-9A-Z]{16}|D:\\\\|C:\\\\Users\\\\)" -- . ':(exclude).git' || $true
~~~

发布前确认以下内容不存在于提交中：.env、API Key、JWT/MCP 密钥、模型权重、日志、会话/记忆、数据库文件、个人路径和真实客户数据。如果曾经把密钥提交过，请先撤销并重新生成，再整理 Git 历史。

项目尚未声明开源许可证；公开发布前请根据你的用途补充合适的 LICENSE 文件。
