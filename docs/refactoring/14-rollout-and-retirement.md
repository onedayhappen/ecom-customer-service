# 14 迁移上线并删除被替代的旧链路

完成真实环境验证和数据切换，再删除已被新实现替代的路径，使项目最终只维护一套有效链路。

前置：完成 [13 跟进外部结果并判断事项何时结束](13-external-progress.md)。 总体约束见 [完整方案](../AGENT_REFACTOR_PLAN.md)。

## 改动位置

`app/api/main.py`、`app/tasks`、`app/execution`、`app/memory`、`app/config/settings.py`、`scripts`、`tests`、`deploy`、`docs/CONCURRENCY_RUNBOOK.md`。

## 实施步骤

1. 把主方案验收场景逐项映射到现有测试和实际结果。固定模型测试程序约束，真实模型加受控工具评估理解质量，隔离 PostgreSQL、Redis 和业务环境验证多实例及故障恢复。
2. 收集关联错误、遗漏诉求、纠错成功、重复追问、证据支持、耗时和成本。权限、确认、对象隔离和幂等用代码断言；出现错误业务提交必须解决后再放量。
3. 演练迁移：先备份，停止受影响用户旧执行和草稿写入，只转换归属与目标明确的历史记录。无法确定的作为历史参考，不自动产生可执行业务操作。
4. API、Worker、前端按同一版本配套切换。同一用户的所有会话使用一致路径，逐步放量；确认旧任务不会与新任务争抢，既有操作记录和回执始终可查询。
5. 演练停止与回退：优先关闭新执行、保留读取和人工处理。新数据不兼容旧程序时不能直接把写流量切回旧链路，也不能通过重放旧队列回退已发生业务。
6. 确认迁移与观察通过后，删除旧 Workflow 回滚入口、强制查询改写、旧自动拆待办分支和重复草稿写入等已无调用的代码、配置与测试。共享工具、证据、确认、审计、记忆和 harness 按调用关系保留；重要回归改测新链路。
7. 更新运行手册与 README，清理已删除文件引用。分步移除切换开关与适配代码，留下必要的历史数据读取和审计能力，不再增加第二套方案文档。

## 验收

- 主方案所有首版必需场景有对应结果；真实模型评测与多实例验证均完成，不能只凭本地模拟测试上线。
- 覆盖受理后崩溃、工具调用期间失联、写入结果未知、重复投递、持续输入、跨会话竞争和人工接管。
- 迁移前后消息、事项来源及业务回执可追溯；不明确旧数据不会自动办理。
- 旧路径退役后导入、默认入口、相关全部测试、前端构建和文件引用检查通过；回退演练不重复提交任何业务操作。

## 本次交付边界

交付可运行的新链路、迁移与恢复命令、真实验收结果，并关闭已替代的旧实现。附件识别、额外排序模型和离线通知仍属后续可选工作。

完成后在 [实施入口](README.md) 更新本项状态、提交编号和验证结果。只完成本篇范围，下一部分单独实施。

## 实现与操作入口

代码提交为 `d6d8431`，分支为 `refactor-pipeline-rollout`。结构版本 20 增加全局放量状态、用户覆盖、控制审计及旧事项转换对应关系四张表。全局支持 `stopped / selected / all`，用户覆盖支持 `enabled / disabled`。PostgreSQL 事务和放量切换用共享/独占锁串行确定切换边界；事项、会话版本继续约束每次工具调用、确认和发布。

使用 `python -m scripts.pipeline_rollout status` 查看，`set` 修改，`import` 转换明确的旧事项。完整命令与操作顺序只维护在 [运行手册](../CONCURRENCY_RUNBOOK.md)。控制命令需部署管理员的数据库凭据，不暴露给用户或模型工具。

暂停时消息仍保存，历史与人工服务继续可用；恢复时按保存位置重新调度，执行编号包含会话控制版本。已预留提交的业务可能完成，只记录真实回执，不自动再次提交。历史导入只接受一条完整有效用户原话、正确归属与指定版本，生成 `paused` 事项；不确定的记录仍保留查询，不能作为自动执行授权。

旧 Workflow 执行器、强制查询改写、旧 API 单消息执行器、旧 Worker/Dispatcher、自动拆待办和任务外订单草稿写入已经移除。`POST /api/v1/chat` 返回 410，旧 Job HTTP 查询入口同样返回 410，历史记录仅保留在数据库用于审计；新前端只使用持续消息入口。通知进程只发布通知，不消费旧业务队列。共享 ReAct、工具、证据、业务幂等、审计和记忆保留。

## 验收与测试对应

下表文件均在 `tests/`。程序保护使用确定性断言；数据库竞争、恢复、HTTP 与浏览器测试使用真实进程和隔离基础设施。

| 完整方案场景 | 测试入口 | 核对内容 |
| --- | --- | --- |
| 执行前、中、后持续输入 | `test_message_intake.py`、`test_coordinated_replies.py`、`test_coordinated_replies_http.py` | 原话不丢、接收位置不倒退、旧结果不覆盖新输入 |
| 一条消息多个诉求 | `test_message_association.py` | 多决定原子提交，引用原消息，缺漏进入兜底 |
| 相似订单、多个问题 | `test_task_candidates.py`、`test_message_association.py` | 有效问题和历史候选，模糊答案不能直接绑定 |
| 误关联与之后的修改 | `test_association_correction.py` | 字段来源、后续版本保留、冲突与旧确认失效 |
| 错误摘要和缓存 | `test_task_memory.py`、`test_short_term_memory.py` | 清理失败重试、过时回写拦截、原话保留 |
| 操作已开始或成功 | `test_task_operations.py`、`test_task_operations_mysql.py` | 原操作回执、进程退出前后核查、未知结果不重放 |
| 跨会话续接和并发修改 | `test_task_candidates.py`、`test_task_memory.py`、`test_task_scheduling.py` | 复用同一事项、版本竞争、历史授权不可重用 |
| 两事项一快一慢 | `test_task_scheduling.py`、`test_coordinated_replies.py` | 独立执行、分批发布、重复通知不重复回答 |
| 条件请求和共同订单 | `test_message_association.py`、`test_task_operations.py` | 条件完整保留，写操作固定参数及订单占用 |
| 部分退款、多包裹、金额 | `test_task_operations.py`、`test_external_progress_mysql.py`、`test_commerce_tools.py` | 商品与数量范围、运单与资金证据、能力不足不扩大对象 |
| 确认前修正或取消 | `test_task_operations_api.py`、`test_association_correction.py` | 已失效令牌拒绝，取消竞争有确定结果 |
| 已受理未结束 | `test_external_progress.py` | 受理、办理中、资金到账分开，道谢不自动结束 |
| 外部更新重复乱序 | `test_external_progress.py`、`test_external_progress_http.py` | 幂等、有界轮询、其他渠道申请不冒充原回执 |
| 人工接管与跨会话 | `test_task_handoffs.py`、`test_task_handoffs_http.py`、`test_platform_browser.py` | 同一事项停止自动写入，交接来源与私有备注隔离 |
| 无知识、历史规则、签收争议 | `test_policy_retrieval.py`、`test_policy_http.py`、`test_external_progress.py` | 适用版本、完整条款、撤回校验、用户陈述与业务事实分开 |
| 越权与恶意资料 | `test_task_execution.py`、`test_task_memory.py`、`test_api_security.py` | 工具名单、对象、证据和权限由代码控制；无归属旧记录禁止读取 |
| 重启、重复投递、超时 | `test_task_scheduling.py`、`test_pipeline_http.py`、`test_task_operations_mysql.py` | 真实 Worker 退出与恢复、旧结果失效、写入不重放 |
| 迁移、停止与恢复 | `test_pipeline_rollout.py`、`test_pipeline_backup.py`、`test_pipeline_http.py` | PostgreSQL/SQLite 迁移重试、备份恢复、跨实例用户控制、审计原子性 |

附件识别尚未开放，首版只接收文本并允许转人工，不能声称已经解析图片、音频或任意链接。

删除的旧多轮、改写、单消息 API 及草稿测试，不再断言已退役行为。它们承担的隔离、历史、连续输入和模型预算回归，分别由上述事项、记忆、执行、回复及真实 HTTP 测试接替。只保留共享能力和历史读取所需的兼容测试。

完整回归发现并修复了本地 JSON 记忆的 Windows 并发路径校验问题：文件被原子替换时只解析稳定的父目录，拒绝路径分隔符、备用数据流和符号链接；跨进程合并重复测试及存储专项 30 项通过。该修复不改变 PostgreSQL 记忆协议。

## 真实模型评测

2026-10-21 使用配置模型 `deepseek-v4-flash`，仅发送合成客服场景，工具返回受控数据，无生产订单写入。

| 项目 | 结果 |
| --- | --- |
| 归属场景 | 10/10；误关联 0，不必要澄清 0，期望决定缺漏 0 |
| 纠错样例 | 明确更正退货原因的 1 个样例通过；不据此推算线上纠错成功率 |
| 执行场景 | 5/5；查询、缺少原因追问、等待外部进度及两类提示注入 |
| 证据与安全 | 5 个执行样例均保存至少 1 条工具证据；跨订单调用和业务写入均为 0 |
| 归属调用耗时与 token | 10 次调用合计 13.108 秒；输入 21066、输出 1709 token；单场景总耗时 0.985–1.765 秒 |
| 执行调用耗时与 token | 14 次调用合计 15.875 秒；输入 44864、输出 1219 token；单场景总耗时 2.282–5.250 秒 |
| 成本 | 报告保存供应商返回的 token；未提供计费价格，费用记为未知，不填写 0 或虚构金额 |

重跑入口为 `python -m scripts.eval_association --live` 和 `python -m scripts.eval_task_execution --live`。JSON 报告保留逐例决定、证据数量、耗时和用量，写入被忽略的 `data/evaluation/`。这是小型冒烟样例，缺漏按期望决定数量计算；不等同于线上准确率、完整诉求召回率或重复追问率。重复提问和纠错传播另由确定性回归覆盖。

## 发布边界

本次执行隔离数据库迁移、备份恢复、真实多实例进程及浏览器演练；没有对生产流量、生产客户或生产订单进行切换。生产放量需按运行手册先停止旧写进程并验证备份，再核对真实业务字段、知识审批和容量。

当前仓库的知识样例不等于审核后的业务条款；生产业务数据未必具备店铺、渠道、商品类别和规则选时需要的所有字段。本机未配置可用的生产 Embedding 权重，真实 Chroma 测试使用受控编码器，证明索引和权限逻辑而非生产检索质量。缺少依据时系统澄清或转人工；正式放量前需配置已审核知识、匹配编码器并完成目标硬件容量验证。

## 已完成验证

| 检查 | 实际结果 |
| --- | --- |
| 最终常规全量回归 `scripts/run_platform_checks.py` | 1131 通过、363 跳过，另有 15 个子测试通过；184.98 秒 |
| 全量隔离集成 `scripts/run_platform_checks.py --integration` | 1468 通过、13 跳过，另有 15 个子测试通过；759.30 秒。实际 PostgreSQL、Redis、MySQL、多进程 API/Worker、HTTP/SSE 和 Edge 浏览器 |
| 最终切换与历史读取专项 | `test_pipeline_rollout.py`、`test_pipeline_backup.py`、`test_session_display.py`，SQLite/PostgreSQL 共 40 通过；26.00 秒 |
| JSON 记忆并发修正专项 | 30 通过；9.78 秒。最终常规全量回归已包含该修正和新增案例 |
| 前端状态测试 | `npm --prefix frontend test`，3 通过 |
| 前端浏览器测试 | `npm --prefix frontend run test:browser`，Edge 共 7 通过；40.9 秒 |
| 前端构建 | `npm --prefix frontend run build` 通过 |
| 静态与部署检查 | Python 编译、Compose 模板配置、Git 空白检查和现有 Markdown 本地链接检查通过 |

全量集成运行期间补充的旧配置兼容、历史归属校验和 JSON 路径修正，已由最终专项及修正后的常规全量回归复核。常规回归跳过需要显式隔离环境的测试；集成仍有独立开启条件或后端限制的测试跳过，不能把跳过计为通过。

常规回归的 2 条警告来自认证测试使用短测试密钥；集成另有 251 条来自现有 MySQL 客户端 `ping(reconnect=True)` 的弃用提示。此次没有失败项。逐轮日志、真实模型报告及浏览器截图保留在被忽略的 `data/evaluation/` 与 `.codex/platform-checks-*`，不提交机器路径、凭据或客户数据。

第 01–14 部分均已分支实现、测试并提交。本部分完成可部署代码和隔离切换演练，生产发布仍按上述边界与运行手册执行。
