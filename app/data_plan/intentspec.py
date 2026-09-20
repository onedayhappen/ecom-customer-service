"""IntentSpec 静态定义：每个意图需要哪些数据、工具、权限、执行路径。

IntentSpec 是**静态定义层**（intent → 需求），与运行时的 Data Plan
（结合 Case 上下文实例化）分离。装配差异显著且稳定的意图独立成类；
同数据源同动作只换对象的（退货/换货/价保）用槽位区分，不拆标签。

字段说明：
- ``tools``      引用 :mod:`app.data_plan.capabilities` 的能力 ID（按装配优先级排序）。
- ``skills``     引用 ``app/skills/definitions/*`` 的 skill 名。
- ``memory``     需要装配的数据源（会话/Case/画像/订单库/商品库/政策库）。
- ``permission`` 权限等级：只读 / 执行前确认 / 升级转人工。
- ``required_slots`` 执行前必须解析的实体/槽位，用于初始化 ``case.pending_slots``。
- ``fallback``   前置条件不满足时的降级策略。
- ``execution``  workflow（确定性脚本）/ react（动态规划）/ rule（决策规则填槽）。
- ``retrievable`` 派生：是否需检索外部数据源（对应已废弃的 requires_retrieval）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.data_plan.capabilities import CAPABILITIES
from app.data_plan.enums import Domain, Execution, Fallback, Intent, Permission


@dataclass(frozen=True)
class MemorySpec:
    """意图需要装配的数据源（均为布尔开关）。"""

    session: bool = False        # 会话记忆（短期上下文，指代消解）
    case: bool = False           # Case 记忆（跨会话业务事实）
    user_profile: bool = False   # 用户长期记忆 / 偏好画像
    order_db: bool = False       # 订单库
    product_db: bool = False     # 商品库
    policy_kb: bool = False      # 政策知识库（退换货/FAQ）

    def sources(self) -> tuple[str, ...]:
        """返回已开启的数据源名（按固定顺序），用于装配与观测。"""
        order = (
            ("session", self.session),
            ("case", self.case),
            ("user_profile", self.user_profile),
            ("order_db", self.order_db),
            ("product_db", self.product_db),
            ("policy_kb", self.policy_kb),
        )
        return tuple(name for name, on in order if on)


@dataclass(frozen=True)
class IntentSpec:
    """一个意图的完整静态需求定义。"""

    intent: Intent
    domain: Domain
    execution: Execution
    description: str
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    memory: MemorySpec = field(default_factory=MemorySpec)
    permission: Permission = Permission.READ_ONLY
    required_slots: tuple[str, ...] = ()
    fallback: Fallback = Fallback.CLARIFY

    @property
    def retrievable(self) -> bool:
        """是否需检索外部数据源（替代已废弃的 requires_retrieval）。

        与"是否含工具"解耦：closure 含 send_satisfaction（触发动作）但仍为 False，
        因为满意度触达是副作用而非检索。
        """
        return any((
            self.memory.order_db,
            self.memory.product_db,
            self.memory.policy_kb,
            self.memory.user_profile,
        ))


# ---------------------------------------------------------------------------
# 17 类 IntentSpec 注册表
# ---------------------------------------------------------------------------

_SPECS: tuple[IntentSpec, ...] = (
    # ===== 售前 =====
    IntentSpec(
        intent=Intent.PRODUCT_INQUIRY,
        domain=Domain.PRESALE,
        execution=Execution.REACT,
        description="商品咨询：参数/价格/优惠/库存/政策/赠品/会员权益等客观信息",
        tools=("query_product", "search_knowledge", "calc_promotion", "query_inventory"),
        memory=MemorySpec(session=True, product_db=True, policy_kb=True),
        fallback=Fallback.CLARIFY,
    ),
    IntentSpec(
        intent=Intent.PRODUCT_RECOMMENDATION,
        domain=Domain.PRESALE,
        execution=Execution.REACT,
        description="商品推荐：依赖用户偏好画像生成个性化方案；无画像则澄清追问",
        tools=("recall_user_memory", "query_product", "recommend_products"),
        skills=("product-recommend",),
        memory=MemorySpec(session=True, user_profile=True, product_db=True),
        fallback=Fallback.CLARIFY,
    ),

    # ===== 订单 =====
    IntentSpec(
        intent=Intent.ORDER_INQUIRY,
        domain=Domain.ORDER,
        execution=Execution.WORKFLOW,
        description="订单查询：状态/明细/金额；未提供订单号则列订单澄清",
        tools=("query_order", "list_user_orders"),
        memory=MemorySpec(session=True, case=True, order_db=True),
        required_slots=("order_id",),
        fallback=Fallback.CLARIFY,
    ),
    IntentSpec(
        intent=Intent.LOGISTICS_INQUIRY,
        domain=Domain.ORDER,
        execution=Execution.WORKFLOW,
        description="物流查询：快递公司/轨迹/到哪；缺订单号先查订单",
        tools=("query_order", "query_logistics", "list_user_orders"),
        skills=("track-order",),
        memory=MemorySpec(session=True, case=True, order_db=True),
        required_slots=("order_id",),
        fallback=Fallback.CLARIFY,
    ),
    IntentSpec(
        intent=Intent.ADDRESS_CHANGE,
        domain=Domain.ORDER,
        execution=Execution.WORKFLOW,
        description="修改地址：校验订单未发货 + 新地址合法性后写入",
        tools=("query_order", "list_user_orders", "validate_address", "update_address"),
        memory=MemorySpec(session=True, case=True, order_db=True),
        permission=Permission.CONFIRM_REQUIRED,
        required_slots=("order_id", "new_address"),
        fallback=Fallback.EXPLAIN,
    ),
    IntentSpec(
        intent=Intent.ORDER_CANCEL,
        domain=Domain.ORDER,
        execution=Execution.WORKFLOW,
        description="取消订单：校验可取消状态后执行，并告知退款去向",
        tools=("query_order", "list_user_orders", "cancel_order"),
        memory=MemorySpec(session=True, case=True, order_db=True),
        permission=Permission.CONFIRM_REQUIRED,
        required_slots=("order_id",),
        fallback=Fallback.EXPLAIN,
    ),
    IntentSpec(
        intent=Intent.EXPEDITE,
        domain=Domain.ORDER,
        execution=Execution.WORKFLOW,
        description="催发货/加急：校验未发货后触发仓库加急",
        tools=("query_order", "expedite_logistics"),
        memory=MemorySpec(session=True, case=True, order_db=True),
        permission=Permission.CONFIRM_REQUIRED,
        required_slots=("order_id",),
        fallback=Fallback.EXPLAIN,
    ),

    # ===== 售后 =====
    IntentSpec(
        intent=Intent.AFTER_SALE_REQUEST,
        domain=Domain.AFTER_SALE,
        execution=Execution.REACT,
        description="售后申请：退/换/价保（槽位路由到 apply_refund/exchange/price_protect）",
        tools=("query_order", "list_user_orders", "search_knowledge",
               "apply_refund", "apply_exchange", "apply_price_protect"),
        skills=("process-return",),
        memory=MemorySpec(session=True, case=True, order_db=True, policy_kb=True),
        permission=Permission.CONFIRM_REQUIRED,
        required_slots=("order_id", "reason"),
        fallback=Fallback.EXPLAIN,
    ),
    IntentSpec(
        intent=Intent.REFUND_STATUS,
        domain=Domain.AFTER_SALE,
        execution=Execution.WORKFLOW,
        description="退款进度：退款单状态/到账节点；无退款单则说明",
        tools=("query_refund", "query_order", "list_user_orders"),
        memory=MemorySpec(session=True, case=True, order_db=True),
        required_slots=("order_id",),
        fallback=Fallback.EXPLAIN,
    ),
    IntentSpec(
        intent=Intent.ISSUE_REPORT,
        domain=Domain.AFTER_SALE,
        execution=Execution.REACT,
        description="问题反馈：故障/货损/货不对板诊断 + 售后条件判断，诊断无果升级",
        tools=("query_order", "list_user_orders", "search_knowledge", "analyze_image"),
        memory=MemorySpec(session=True, case=True, order_db=True, policy_kb=True),
        required_slots=("order_id",),
        fallback=Fallback.ESCALATE,
    ),
    IntentSpec(
        intent=Intent.INVOICE,
        domain=Domain.AFTER_SALE,
        execution=Execution.WORKFLOW,
        description="发票管理：开票/查票/重开；需订单号 + 抬头类型 + 抬头信息",
        tools=("invoice_service", "query_order"),
        memory=MemorySpec(session=True, case=True, order_db=True),
        permission=Permission.CONFIRM_REQUIRED,
        required_slots=("order_id", "invoice_type", "title"),
        fallback=Fallback.EXPLAIN,
    ),
    IntentSpec(
        intent=Intent.COMPLAINT,
        domain=Domain.AFTER_SALE,
        execution=Execution.REACT,
        description="投诉：安抚 + 核实 + 工单/转人工升级",
        tools=("query_order", "search_knowledge", "recall_user_memory", "escalate_to_human"),
        memory=MemorySpec(session=True, case=True, user_profile=True, order_db=True, policy_kb=True),
        permission=Permission.ESCALATE,
        required_slots=("order_id",),
        fallback=Fallback.ESCALATE,
    ),

    # ===== 对话流（并入 Workflow：无业务工具，回复由模型生成/模板兜底） =====
    IntentSpec(
        intent=Intent.CONFIRMATION,
        domain=Domain.DIALOG,
        execution=Execution.WORKFLOW,
        description="确认/认可：触发决策规则执行待确认动作（case.pending_slots == confirmation）",
        memory=MemorySpec(session=True, case=True),
        fallback=Fallback.NORMAL_REPLY,
    ),
    IntentSpec(
        intent=Intent.ANSWER_QUESTION,
        domain=Domain.DIALOG,
        execution=Execution.WORKFLOW,
        description="回答客服提问：填 case.pending_slots 槽位；无活跃流程则普通回应",
        memory=MemorySpec(session=True, case=True),
        fallback=Fallback.NORMAL_REPLY,
    ),
    IntentSpec(
        intent=Intent.CLOSURE,
        domain=Domain.DIALOG,
        execution=Execution.WORKFLOW,
        description="收尾（感谢/告别）：触发满意度触达 + 会话关闭",
        tools=("send_satisfaction",),
        memory=MemorySpec(session=True),
        fallback=Fallback.NONE,
    ),
    IntentSpec(
        intent=Intent.GREETINGS,
        domain=Domain.DIALOG,
        execution=Execution.WORKFLOW,
        description="问候：开场寒暄，无实质诉求",
        memory=MemorySpec(session=True),
        fallback=Fallback.NONE,
    ),

    # ===== 兜底 =====
    IntentSpec(
        intent=Intent.OTHERS,
        domain=Domain.FALLBACK,
        execution=Execution.REACT,
        description="其他/无法归类：澄清或转人工兜底",
        memory=MemorySpec(session=True),
        fallback=Fallback.CLARIFY,
    ),
)


# intent → spec 的查询表（O(1) 查表，IntentSpec 是确定性映射，不靠 LLM）
SPECS: dict[Intent, IntentSpec] = {spec.intent: spec for spec in _SPECS}


def get_intentspec(intent: Intent | str) -> IntentSpec:
    """按意图查 IntentSpec；未知意图抛 KeyError（配置完备性由测试保障）。

    接受 ``Intent`` 枚举或分类器输出的字符串值。
    """
    if isinstance(intent, str):
        intent = Intent(intent)
    return SPECS[intent]


def validate_specs() -> list[str]:
    """返回配置错误列表（空列表 = 全部合法）。

    在导入/测试阶段调用，保障：
    1. 17 类全覆盖、无重复；
    2. 每个 spec 引用的工具能力都存在于能力目录；
    3. 写操作能力只出现在 CONFIRM_REQUIRED 权限下（安全约束）；
    4. 对话流（RULE）不装配业务检索工具。
    """
    errors: list[str] = []

    all_intents = set(Intent)
    defined = set(SPECS)
    if defined != all_intents:
        missing = sorted(i.value for i in all_intents - defined)
        errors.append(f"IntentSpec 未覆盖: {missing}")

    from app.data_plan.capabilities import MUTATING_CAPABILITIES
    for spec in SPECS.values():
        # 工具能力必须存在于目录
        for tool in spec.tools:
            if tool not in CAPABILITIES:
                errors.append(f"{spec.intent.value}: 引用未知工具 {tool}")
        # 写操作能力只能出现在需确认权限下
        for tool in spec.tools:
            if tool in MUTATING_CAPABILITIES and spec.permission != Permission.CONFIRM_REQUIRED:
                errors.append(f"{spec.intent.value}: 写操作工具 {tool} 需 CONFIRM_REQUIRED 权限")
        # 对话流（DIALOG 域）不装配业务检索工具（仅允许满意度触达）
        if spec.domain == Domain.DIALOG and any(t != "send_satisfaction" for t in spec.tools):
            errors.append(f"{spec.intent.value}: 对话流不应装配业务工具 {spec.tools}")

    return errors
