# 项目结构

仓库按运行边界组织，提交到 GitHub 的内容只包含源代码、配置模板、测试、文档和意图识别数据集。密钥、模型权重、数据库文件、会话和日志留在本地或 Docker 挂载目录中。

```text
ecom-customer-service/
├── app/
│   ├── api/              FastAPI 路由、认证、中间件
│   ├── agents/           Agent 入口和响应组装
│   ├── config/           配置模型和环境变量
│   ├── data/             订单、物流、商品 DAO 与 service
│   ├── data_plan/        意图规格、能力规划和工具白名单
│   ├── execution/        Workflow、ReAct 和执行器
│   ├── harness/          评估和故障注入运行器
│   ├── intent/           意图分类、规则回退和查询改写
│   ├── mcp/              MCP 客户端、服务端和认证
│   ├── memory/           长期记忆、检索和 Case
│   ├── observability/    审计、追踪、指标和脱敏
│   ├── prompts/          Agent、意图和工具提示词
│   ├── rag/              知识库、Embedding 和索引更新
│   ├── reliability/      证据校验、失败策略和幂等变更
│   ├── runtime/          并发、队列和运行时协调
│   ├── session/          会话存储、生命周期和上下文预算
│   ├── skills/           可复用业务技能
│   └── tools/            订单、物流、商品、退款等工具
├── data/
│   ├── fine_tuning/      意图微调训练集及 ShareGPT 转换集
│   └── evaluation/       意图独立测试集
├── frontend/             React/Vite 客服工作台
├── scripts/              启动 Worker、数据库初始化、训练和评测脚本
├── tests/                单元测试、API 测试和可靠性测试
├── docs/                 架构、开发和发布说明
├── Dockerfile            后端镜像
├── docker-compose.yml    完整本地环境（PostgreSQL、Redis、MySQL、后端、前端）
├── requirements.txt      主服务依赖
└── requirements-intent.txt  意图模型训练的可选依赖
```

训练和评估的入口文件：

- `data/fine_tuning/multiturn_intent_sft/data_v3.jsonl`
- `data/fine_tuning/multiturn_intent_sft/data_v3_sharegpt.jsonl`
- `data/evaluation/multiturn_intent_eval_v3.jsonl`
- `scripts/train_intent_lora.py`
- `scripts/eval_intent_model.py`

上传前运行：

```powershell
git status --short
git diff --check
python -m pytest -q
Set-Location frontend
npm ci
npm run build
```

`.env`、`model/`、`.docker-data/`、`logs/`、会话数据和评估报告已加入忽略规则，不应提交到公共仓库。
