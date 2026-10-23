# 核心工具实现与运行配置

本文说明现有 38 个核心工具的实现与运行配置。[工具设计](TOOL_DESIGN.md)已调整为待落地的 33 个基础工具及 15 个扩展工具，当前代码尚未按该新契约改造。


## 1. 代码位置

| 位置 | 职责 |
| --- | --- |
| `app/tools/contracts.py`、`catalog.py` | 38 个工具的参数类型、说明和副作用声明；禁止额外参数与隐式类型转换 |
| `app/tools/*_tools.py`、`dispatch.py` | 按用户、商品、订单、优惠、售后、发票、政策、事项划分的调用入口 |
| `app/tools/selection.py`、`registry.py` | 按事项提供工具子集，统一校验身份与参数 |
| `app/services/commerce/reads.py` | 新版业务表查询与归属校验 |
| `app/services/commerce/rules.py` | 资格判断、冻结券规则、促销计算与政策检索 |
| `app/services/commerce/writes.py` | 准备内容、状态快照、加密、事务写入与幂等回执 |
| `app/data/business_store.py` | MySQL 参数化查询、只读事务与写事务 |
| `app/tasks/operations.py` | 用户确认、事项版本校验、业务提交及原回执核实 |
| `app/tasks/tool_facts.py` | 新工具结果的客户回复展示；不把模型草稿当作业务事实 |

本地目录共 38 个工具。独立 MCP 只有已验证用户身份，没有可用于提交的事项上下文，因此提供 29 个读取／判断／试算工具。其余 9 个工具在事项执行器内调用。MCP 元数据不是权限凭证；服务端仍逐次检查身份与对象归属。

## 2. 配置与迁移

安装 `requirements.txt`，在 `.env` 配置 `ORDER_DB_DSN`、`API_JWT_SECRET` 和现有事项运行连接，然后执行：

```powershell
python -m scripts.migrate_mysql_business_schema
python -m scripts.migrate_mysql_business_schema --check
```

脚本按 `schema_migrations` 顺序执行 001、002、003。已有前两个版本的数据库只增加 003 中的 7 张配套表；新建空库依次执行三版。迁移不自动生成真实业务数据、用户领券记录或默认资格政策。

准备操作使用 AES-256-GCM 保存敏感参数，需配置：

```dotenv
BUSINESS_DATA_KEYS={"1":"<32字节随机密钥的URL-safe Base64编码>"}
BUSINESS_DATA_KEY_VERSION=1
```

密钥通过系统安全随机数产生，保存在本地环境配置或密钥管理服务，不提交到 Git。API 和 task worker 必须使用相同密钥，轮换后保留旧版本以读取历史记录。未配置密钥时读取工具可用，准备操作会明确返回配置不可用。

地址密文格式为 `12 字节 nonce + AESGCM 密文及 tag`，AAD 为 `address:{user_id}:{field}`，field 分别为 `recipient_name`、`phone`、`detail`。地址写入方必须遵守相同约定；改址准备会核实目标地址可以解密。发票抬头和税号分别使用 `invoice-title:{user_id}`、`invoice-tax:{user_id}`。完整目标地址及发票信息只在登录用户的确认卡片中显示，工具结果和事项回执保存脱敏摘要。

## 3. 可执行业务规则

`business_policy_versions.rules` 使用严格的 JSON 契约。`schema_version=1`；未知字段、未知版本、缺失规则、多个同时有效版本均不能自动放行。

| 字段 | 类型／含义 |
| --- | --- |
| `schema_version` | 固定整数 1 |
| `time_basis` | `request`、`order_created` 或 `delivered`；决定有效版本选择时间，默认 request |
| `allowed_statuses` | 该操作允许的订单状态数组，默认空数组 |
| `window_days` | 售后签收后期限；为空不应用该期限。要求完整签收依据 |
| `requires_reason_review` | 为 true 时需人工审核原因，自动资格返回 unknown |
| `refund_shipping` | 为 true 时当前实现返回 unknown；运费分摊依据尚未接入 |
| `local_authority` | 本地具有取消／改址／撤回控制权；默认 false |
| `inventory_managed`、`coupons_managed` | 取消订单时库存预留和券占用须纳入同一事务；默认 false |
| `threshold_basis` | 当前只支持 `goods_after_promotions` |
| `coupon_stacking`、`promotions_with_coupons` | 多券叠加、活动与券叠加许可；默认 false |
| `rag_policy_id`、`rag_policy_version` | 对应政策检索结果的 source_id 和 version；不一致会显示冲突，回复发布时拒绝引用 |

可执行 action：`refund_only`、`return_refund`、`invoice`、`cancel_order`、`change_address`、`after_sale_cancel`、`pricing`。这些字段由业务管理方维护，模型不能修改。带 channel 的规则要求可信渠道上下文；目前不能自动采用该规则。

优惠 kind 目前支持 `amount_off` 和 `percent_off`。`discount_bps` 表示减免比例的基点，例如 1000 表示减免 10%，不是支付 10%。适用范围类型为 `all`（scope_id 为 `*`）、`merchant`、`product`、`sku`。活动按 priority 降序执行，同组互斥；券按指定组合顺序执行并逐行分摊至分，不擅自替换组合。SKU 列价、优惠、订单必须使用相同币种。

领取成功时将券模板和范围保存到 `coupon_claim_rules`，后续按此快照解释持券承诺。领取事务锁定券模板并重查总量／个人限领；同一事项来源请求使用同一服务端生成的请求标识。

## 4. 当前执行边界

- 仅从 MySQL 读取业务事实，不使用旧工具或假数据回退。分页固定 20 条；签名游标绑定用户、筛选和数据快照，有效期 15 分钟。结果变动要求从第一页重新查；单次查询超过 10000 条返回范围过大。
- 订单、售后、发票必须属于当前用户；订单引用包含平台与订单号。公共商品未明确平台时，价格／领券判断需要先澄清，事项中的平台须能通过本人订单或有效账号绑定核实。
- `prepare_after_sale` 提交待审核申请与商品行；不调用支付退款接口。`prepare_invoice` 提交加密的待处理申请；不生成正式发票。
- 改址仅适用于本地控制且尚未进入发货流程的订单。取消仅适用于未支付、无进行中支付、无发货、库存预留完整且可释放券占用的订单。售后撤回仅适用于有本地提交依据、尚未受理且无有效退款的申请。
- 确认有效期 10 分钟；确认绑定事项、用户、操作参数、订单平台及业务快照，提交时再次核验规则和数据。MySQL 的业务变更、事件与回执在同一事务提交；超时通过原 operation_id 查询，不盲目重复写入。
- `ask_user` 保存具体问题并停止本轮；`handoff_to_human` 创建或复用真实工单；`read_task_context` 只读取当前授权事项及其工单记录。
- 政策文本来自现有经审核的 RAG 索引；未配置索引或检索失败时返回依据不足。18 个扩展工具、外部退款／正式开票、逆向物流、自动外部进度跟进不在此次实现范围。

## 5. 验证命令

```powershell
python scripts/run_platform_checks.py
```

核心业务集成测试需要一个允许创建临时库的测试 MySQL 管理连接：

```powershell
$env:CORE_TOOLS_TEST_MYSQL_ADMIN='mysql://测试账号:测试密码@127.0.0.1:3307/mysql'
python scripts/run_platform_checks.py tests/test_core_tools.py tests/test_core_task_tools.py
```

测试创建独立 `test_core_tools_<随机值>` 数据库，完成迁移与并发／事务／权限验证后仅删除该临时库。没有显式配置此变量时跳过 MySQL 测试。其他 PostgreSQL 集成测试仍使用项目专用集成环境。
